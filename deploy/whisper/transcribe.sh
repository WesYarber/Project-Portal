#!/bin/sh
# One argument: an audio file in any format ffmpeg can read (a phone's
# MediaRecorder produces audio/webm opus or audio/mp4). Prints the transcript
# on stdout; anything else goes to stderr so the caller can keep them apart.
set -e
ffmpeg -y -loglevel error -i "$1" -ar 16000 -ac 1 -f wav /tmp/in.wav
whisper-cli -m /models/ggml-base.en.bin -f /tmp/in.wav -np -nt 2>/dev/null
