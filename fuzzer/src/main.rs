use serde::{Deserialize, Deserializer, Serialize, Serializer};

use libafl::{
    corpus::{InMemoryCorpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::{Executor, ExitKind, ForkserverExecutor, HasObservers, StdChildArgs},
    feedbacks::{
        new_hash_feedback::HashSetState, Feedback, NewHashFeedbackMetadata, StateInitializer,
    },
    inputs::BytesInput,
    inputs::ToTargetBytes,
    monitors::SimpleMonitor,
    mutators::{havoc_mutations, HavocScheduledMutator},
    observers::{ObserversTuple, StdMapObserver, StdOutObserver},
    schedulers::QueueScheduler,
    stages::StdMutationalStage,
    state::{HasExecutions, StdState},
    Error, Fuzzer, HasNamedMetadata, HasTargetBytesConverter, StdFuzzer,
};

use libafl_bolts::{
    rands::StdRand,
    shmem::{ShMem, ShMemProvider, UnixShMemProvider},
    tuples::{tuple_list, Handle, Handled, MatchName, MatchNameRef, RefIndexable},
    AsSliceMut, InputLocation, Named, StdTargetArgs,
};
use std::{
    borrow::Cow,
    collections::HashMap,
    fs,
    hash::{DefaultHasher, Hasher},
    path::PathBuf,
    process::Command,
};

use core::{fmt::Debug, hash::Hash};

type StandardMapObserver<'a> = StdMapObserver<'a, u8, false>;

#[derive(Serialize, Deserialize, Debug, Clone)]
struct StdOutDiffObjective {
    name: Cow<'static, str>,
    stdout_observer_handles: Vec<Handle<StdOutObserver>>,
}

impl<S> StateInitializer<S> for StdOutDiffObjective {
    fn init_state(&mut self, _: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

impl Named for StdOutDiffObjective {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl StdOutDiffObjective {
    fn new(stdout_observers: &Vec<StdOutObserver>) -> Self {
        Self {
            name: Cow::from("stdout diff objective"),
            stdout_observer_handles: stdout_observers.iter().map(|o| o.handle()).collect(),
        }
    }
}

impl<EM, I, OT, S> Feedback<EM, I, OT, S> for StdOutDiffObjective
where
    OT: MatchName,
{
    fn is_interesting(
        &mut self,
        _: &mut S,
        _: &mut EM,
        _: &I,
        observers: &OT,
        _: &ExitKind,
    ) -> Result<bool, Error> {
        let mut second_iterator = self.stdout_observer_handles.iter();
        second_iterator.next();
        Ok(self
            .stdout_observer_handles
            .iter()
            .zip(second_iterator)
            .map(|(o1_handle, o2_handle)| {
                match (
                    &observers
                        .get(&o1_handle)
                        .expect("couldn't find stdout observer 1")
                        .output,
                    &observers
                        .get(&o2_handle)
                        .expect("couldn't find stdout observer 2")
                        .output,
                ) {
                    (None, None) => false,
                    (Some(_), None) | (None, Some(_)) => true,
                    (Some(output1), Some(output2)) => output1 != output2,
                }
            })
            .any(|x| x))
    }
    
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct FineGrainedDeltaDiversity<'a> {
    name: Cow<'static, str>,
    map_observer_handles: Vec<Handle<StandardMapObserver<'a>>>,
}

impl<'a> FineGrainedDeltaDiversity<'a> {
    fn new(map_observer_handles: Vec<Handle<StandardMapObserver<'a>>>) -> Self {
        Self {
            name: Cow::from("fine-grained delta diversity"),
            map_observer_handles,
        }
    }
}

impl Named for FineGrainedDeltaDiversity<'_> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<EM, I, OT, S> Feedback<EM, I, OT, S> for FineGrainedDeltaDiversity<'_>
where
    OT: MatchName,
    S: HasNamedMetadata,
{
    fn is_interesting(
        &mut self,
        state: &mut S,
        _manager: &mut EM,
        _input: &I,
        observers: &OT,
        _exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        let mut hasher = DefaultHasher::new();
        for map_observer_handle in &self.map_observer_handles {
            observers
                .get(&map_observer_handle)
                .expect("observer handle lookup failed")
                .hash(&mut hasher)
        }
        Ok(state
            .named_metadata_map_mut()
            .get_mut::<NewHashFeedbackMetadata>(&self.name)
            .expect("couldn't get FineGrainedDeltaDiversity state hashset")
            .update_hash_set(hasher.finish())
            .expect("couldn't hash"))
    }
}

impl<S> StateInitializer<S> for FineGrainedDeltaDiversity<'_>
where
    S: HasNamedMetadata,
{
    fn init_state(&mut self, state: &mut S) -> Result<(), Error> {
        state
            .add_named_metadata_checked(&self.name, NewHashFeedbackMetadata::with_capacity(4096))
            .expect("couldn't add named metadata");
        Ok(())
    }
}

struct ForkserverExecutors<I, OT, S, SHM> {
    executors: Vec<ForkserverExecutor<I, OT, S, SHM>>,
}

impl<I, OT, S, SHM> Serialize for ForkserverExecutors<I, OT, S, SHM> {
    fn serialize<SE>(&self, _: SE) -> Result<SE::Ok, SE::Error>
    where
        SE: Serializer,
    {
        panic!("stop trying to do this.")
    }
}

impl<'de, I, OT, S, SHM> Deserialize<'de> for ForkserverExecutors<I, OT, S, SHM> {
    fn deserialize<D>(_: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        panic!("stop trying to do this.")
    }
}

impl<I, OT, S, SHM> ObserversTuple<I, S> for ForkserverExecutors<I, OT, S, SHM>
where
    OT: ObserversTuple<I, S>,
{
    fn pre_exec_all(&mut self, state: &mut S, input: &I) -> Result<(), Error> {
        for executor in &mut self.executors {
            if !executor.observers_mut().pre_exec_all(state, input).is_ok() {
                panic!()
            }
        }
        Ok(())
    }
    fn post_exec_all(
        &mut self,
        state: &mut S,
        input: &I,
        exit_kind: &ExitKind,
    ) -> Result<(), Error> {
        for executor in &mut self.executors {
            if !executor
                .observers_mut()
                .post_exec_all(state, input, exit_kind)
                .is_ok()
            {
                panic!()
            }
        }
        Ok(())
    }

    fn pre_exec_child_all(&mut self, _: &mut S, _: &I) -> Result<(), Error> {
        // handled by ForkserverExecutor::run_target
        Ok(())
    }

    fn post_exec_child_all(&mut self, _: &mut S, _: &I, _: &ExitKind) -> Result<(), Error> {
        // handled by ForkserverExecutor::run_target
        Ok(())
    }
}

#[expect(deprecated)]
impl<I, OT, S, SHM> MatchName for ForkserverExecutors<I, OT, S, SHM>
where
    OT: ObserversTuple<I, S> + MatchName,
{
    fn match_name<T>(&self, name: &str) -> Option<&T> {
        for executor in &self.executors {
            #[allow(deprecated)]
            if let Some(x) = executor.observers().match_name::<T>(name) {
                return Some(unsafe { &*(x as *const T) });
            }
        }
        None
    }
    fn match_name_mut<T>(&mut self, name: &str) -> Option<&mut T> {
        for executor in &mut self.executors {
            if let Some(x) = executor.observers_mut().match_name_mut::<T>(name) {
                return Some(unsafe { &mut *(x as *mut T) });
            }
        }
        None
    }
}

struct DifferentialExecutor<I, OT, S, SHM> {
    executors: ForkserverExecutors<I, OT, S, SHM>,
}

impl<I, OT, S, SHM> HasObservers for DifferentialExecutor<I, OT, S, SHM> {
    type Observers = ForkserverExecutors<I, OT, S, SHM>;
    fn observers(&self) -> RefIndexable<&Self::Observers, Self::Observers> {
        RefIndexable::from(&self.executors)
    }
    fn observers_mut(&mut self) -> RefIndexable<&mut Self::Observers, Self::Observers> {
        RefIndexable::from(&mut self.executors)
    }
}

impl<I, OT, S, SHM> DifferentialExecutor<I, OT, S, SHM> {
    fn new(executors: Vec<ForkserverExecutor<I, OT, S, SHM>>) -> Self {
        Self {
            executors: ForkserverExecutors { executors },
        }
    }
}

impl<EM, I, S, Z, OT, SHM> Executor<EM, I, S, Z> for DifferentialExecutor<I, OT, S, SHM>
where
    OT: ObserversTuple<I, S>,
    S: HasExecutions,
    SHM: ShMem,
    Z: HasTargetBytesConverter,
    <Z as HasTargetBytesConverter>::Converter: ToTargetBytes<I>,
{
    fn run_target(
        &mut self,
        fuzzer: &mut Z,
        state: &mut S,
        mgr: &mut EM,
        input: &I,
    ) -> Result<ExitKind, Error> {
        Ok(
            if self
                .executors
                .executors
                .iter_mut()
                .map(|e| {
                    e.run_target(fuzzer, state, mgr, input)
                        .expect("couldn't run target")
                })
                .all(|x| match x {
                    ExitKind::Ok => true,
                    ExitKind::Crash | ExitKind::Oom | ExitKind::Timeout => false,
                    ExitKind::Diff {
                        primary: _,
                        secondary: _,
                    } => panic!("don't use diff with this executor"),
                })
            {
                ExitKind::Ok
            } else {
                ExitKind::Crash
            },
        )
    }
}

#[derive(Deserialize, Debug, Clone)]
struct TargetConfig {
    binary_path: Cow<'static, str>,
}

#[derive(Deserialize, Debug)]
struct Config {
    #[serde(flatten)]
    target_configs: HashMap<Cow<'static, str>, TargetConfig>,
}

fn main() {
    let config: Config =
        toml::from_str(&fs::read_to_string("config.toml").expect("couldn't read config.toml"))
            .expect("couldn't parse config.toml");

    let target_names: Vec<_> = config.target_configs.keys().collect();
    let target_configs: Vec<_> = target_names
        .iter()
        .map(|name| config.target_configs[name.as_ref()].clone())
        .collect();

    let mut shmem_provider = UnixShMemProvider::new().expect("couldn't make shmem provider");

    let map_sizes: Vec<usize> = target_configs
        .iter()
        .map(|target_config| {
            str::from_utf8(
                &Command::new(target_config.binary_path.as_ref())
                    .env("AFL_DUMP_MAP_SIZE", "1")
                    .output()
                    .expect("couldn't get target map size")
                    .stdout,
            )
            .expect("AFL_DUMP_MAP_SIZE spit out invalid utf8, somehow")
            .trim()
            .parse()
            .expect("AFL_DUMP_MAP_SIZE spit out a non-numeric string, somehow")
        })
        .collect();

    let mut shmems: Vec<_> = map_sizes
        .iter()
        .map(|map_size| {
            shmem_provider
                .new_shmem(*map_size)
                .expect("couldn't get shmem")
        })
        .collect();

    let shmem_ids: Vec<_> = shmems.iter().map(|shmem| shmem.id().to_string()).collect();

    let map_observers: Vec<_> = target_names
        .iter()
        .zip(shmems.iter_mut())
        .map(|(target_name, shmem)| unsafe {
            StdMapObserver::new(
                target_name.to_owned().clone().into_owned() + "_map_observer",
                shmem.as_slice_mut(),
            )
        })
        .collect();

    let stdout_observers: Vec<_> = target_names
        .iter()
        .map(|target_name| {
            StdOutObserver::new(
                (target_name.to_owned().clone().into_owned() + "_stdout_observer").into(),
            )
            .expect("couldn't get program stdout")
        })
        .collect();

    let mut feedback =
        FineGrainedDeltaDiversity::new(map_observers.iter().map(|o| o.handle()).collect());

    let mut objective = StdOutDiffObjective::new(&stdout_observers);

    let mut state = StdState::new(
        StdRand::new(),
        InMemoryCorpus::<BytesInput>::new(),
        OnDiskCorpus::new(PathBuf::from("../results")).unwrap(),
        &mut feedback,
        &mut objective,
    )
    .expect("couldn't make the fuzzer state");

    let monitor = SimpleMonitor::new(|s| println!("{s}"));

    let mut mgr = SimpleEventManager::new(monitor);

    let scheduler = QueueScheduler::new();

    let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);

    let mut executor = DifferentialExecutor::new(
        target_configs
            .iter()
            .zip(map_observers.into_iter())
            .zip(stdout_observers.into_iter())
            .zip(map_sizes.into_iter())
            .zip(shmem_ids.iter())
            .map(
                |((((target_config, map_observer), stdout_observer), map_size), shmem_id)| {
                    unsafe { std::env::set_var("__AFL_SHM_ID", shmem_id) };
                    ForkserverExecutor::builder()
                        .program(target_config.binary_path.to_string())
                        .env("__AFL_SHM_ID", shmem_id)
                        .coverage_map_size(
                            map_size + 1024, /* Padding required for some fucking reason */
                        )
                        .stdout_observer(stdout_observer.handle())
                        .input(InputLocation::StdIn { input_file: None })
                        .timeout(std::time::Duration::from_secs(5))    // Added a 5s timeout
                        .build_dynamic_map(map_observer, tuple_list!(stdout_observer))
                        .expect("couldn't make forkserverexecutor")
                },
            )
            .collect(),
    );

    state
        .load_initial_inputs(&mut fuzzer, &mut executor, &mut mgr, &["../input".into()])
        .expect("Failed to load the initial corpus");

    let mutator = HavocScheduledMutator::new(havoc_mutations());
    let mut stages = tuple_list!(StdMutationalStage::new(mutator));

    loop {
        fuzzer
            .fuzz_one(&mut stages, &mut executor, &mut state, &mut mgr)
            .expect("fuzzer failed to fuzz");
    }
}
