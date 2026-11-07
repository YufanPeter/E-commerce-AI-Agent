# ASR Voice Input Test Cases

[English](asr_voice_input_cases.md) | [简体中文](asr_voice_input_cases.zh-CN.md)

## First authorization

Precondition: the app is newly installed and has no speech-recognition or microphone permission.

Steps:

1. Tap the microphone button beside the input field.
2. Grant speech-recognition and microphone permissions when prompted.
3. Say: `推荐一款适合油皮的洗面奶`.

Expected:

- The microphone button switches to its recording state.
- Recognized text appears in the input field in real time.
- Sending stops recording and reuses the existing text, RAG, and streaming-response path.

## Permission denied

Precondition: speech-recognition or microphone permission is denied.

Steps:

1. Tap the microphone button.

Expected:

- Recording does not start.
- A clear permission message appears below the input field.
- Text entry and sending remain available.

## Manual stop

Precondition: permissions are granted and recording is active.

Steps:

1. Tap the microphone button again.

Expected:

- Recording stops immediately and the normal microphone icon returns.
- Recognized text remains editable and can be sent.
