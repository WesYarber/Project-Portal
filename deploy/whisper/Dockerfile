# Whisper.cpp rebuilt for this machine's own CPU.
#
# The prebuilt ghcr.io/ggml-org/whisper.cpp:main image dies with "Illegal
# instruction" on any CPU without AVX-512 (it was first hit on a Ryzen 7 7735HS,
# which has AVX2 and no AVX-512) because its
# single libggml-cpu variant was compiled for a newer instruction set. The
# source tree and the ggml-base.en.bin model ride inside that same image, so
# this Dockerfile rebuilds whisper-cli from them with GGML_NATIVE=ON - tuned to
# whatever CPU runs the build, which is the CPU that will run the binary.
#
# Build (a few minutes, once):
#   docker build -t portal-whisper:latest deploy/whisper/
#
# Run (what app/transcribe.py does):
#   docker run --rm --network none -v <dir>:/audio:ro portal-whisper:latest /audio/<file>
FROM ghcr.io/ggml-org/whisper.cpp:main AS src

FROM debian:stable-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*
COPY --from=src /app /src
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON \
      -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF \
 && cmake --build /build -j"$(nproc)" --target whisper-cli

FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=src /app/models/ggml-base.en.bin /models/ggml-base.en.bin
COPY transcribe.sh /usr/local/bin/transcribe.sh
RUN chmod +x /usr/local/bin/transcribe.sh
ENTRYPOINT ["/usr/local/bin/transcribe.sh"]
