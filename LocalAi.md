# Ollama + Qwen3 Coder Setup

## 1. Install Ollama
- Go to **ollama.com/download** and grab the Windows installer
- Run it and finish the install (no git needed)
- Ollama runs in the background and uses your NVIDIA GPU automatically

## 2. Check it works
Open a terminal (PowerShell or Command Prompt):

```
ollama --version
```

## 3. Run Qwen3 Coder
```
ollama run qwen3-coder
```

The first run downloads the model, then drops you into a chat prompt. Type `/bye` to exit.

## Notes
- The default `qwen3-coder` tag is a large model (roughly 19 GB download). If your VRAM is smaller, Ollama will split it between GPU and system RAM, which works but is slower.
- Run `ollama list` to see the exact name and tag of what you downloaded.

## Useful commands
```
ollama list              # see downloaded models
ollama ps                # see what's loaded and whether it's on GPU
ollama pull <model>      # download without running
ollama rm <model>        # delete a model
```
