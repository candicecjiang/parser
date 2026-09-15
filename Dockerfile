FROM debian:13

# Install system packages and clean up
RUN apt update && apt install -y \
	curl python3 python3-pip python3-venv python3-dev \
	build-essential cmake g++ clang llvm pkg-config \
	graphviz libgraphviz-dev \
	afl++ \ 
    zlib1g-dev libgmp-dev \
	libpng-dev \
	vim less git && \ 
    apt clean

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
	. $HOME/.cargo/env && \
	rustup default nightly
ENV PATH="/root/.cargo/bin:${PATH}"

# Create and activate Python virtual environment
RUN python3 -m venv /venv
ENV PATH="/venv/bin:${PATH}"

# Upgrade pip and install Python dependencies
RUN /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install kaitaistruct Pillow pypng crc python-afl watchdog google-genai python-dotenv

# Download and install Kaitai Struct Compiler, then clean up
RUN curl -LO https://github.com/kaitai-io/kaitai_struct_compiler/releases/download/0.11/kaitai-struct-compiler_0.11_all.deb && \
    apt install -y ./kaitai-struct-compiler_0.11_all.deb && \
    rm kaitai-struct-compiler_0.11_all.deb

# Base working directory
WORKDIR /app

# Copy entrypoint script
COPY entrypoint.sh .
RUN chmod +x ./entrypoint.sh

# Copy the Rust fuzzer source & build it
COPY fuzzer/ fuzzer/
WORKDIR /app/fuzzer
RUN cargo build
WORKDIR /app

# Copy test inputs
COPY input/ input/

# Copy the modular harnesses and make them executable
COPY harnesses/ harnesses/
RUN chmod +x harnesses/png/harness_*.py

# Clone all kaitai struct formats, compile what we need into the harnesses folder, then clean up
RUN git clone https://github.com/kaitai-io/kaitai_struct_formats.git /tmp/formats && \
    cd harnesses/png && \
    ksc -t python /tmp/formats/image/icc_4.ksy && \
    ksc -t python /tmp/formats/image/exif.ksy && \
    rm -rf /tmp/formats

# Ensure output directory exists
RUN mkdir -p results

CMD ["./entrypoint.sh"]
