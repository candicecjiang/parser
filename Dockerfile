FROM ubuntu:latest

# Install system packages and clean up
RUN apt-get update && \
    apt-get install -y curl python3 python3-pip python3-venv graphviz libgraphviz-dev pkg-config \ 
    python3-dev build-essential cmake g++ zlib1g-dev libgmp-dev&& \ 
    apt-get clean

# Create and activate Python virtual environment
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Upgrade pip and install Python dependencies
RUN /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install kaitaistruct Pillow pypng && \
    /venv/bin/pip install --no-cache-dir fuzzingbook

# Download and install Kaitai Struct Compiler, then clean up
RUN curl -LO https://github.com/kaitai-io/kaitai_struct_compiler/releases/download/0.10/kaitai-struct-compiler_0.10_all.deb && \
    apt-get install -y ./kaitai-struct-compiler_0.10_all.deb && \
    rm kaitai-struct-compiler_0.10_all.deb

# Copy project files into the container
COPY png.ksy test.png png.py fuzz_png.py flower.jpg /
COPY png/test.png png/test.png