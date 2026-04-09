# Screenshot to LaTeX Agent

A Windows desktop app that converts equation screenshots to LaTeX and copies the result to your clipboard — ready to paste into Word or any equation editor.

## Workflow

1. Capture an equation screenshot (snipping overlay or existing clipboard image).
2. The image is sent to an OpenAI vision model.
3. LaTeX is returned and copied to the clipboard automatically.
4. Paste into Word's equation input or any LaTeX editor.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set your API key:

```env
OPENAI_API_KEY=your_api_key_here
```

## Run

```powershell
python app.py
```

## Usage

| Action | How |
|---|---|
| New screenshot | Click **New Screenshot** or press `Ctrl+Alt+S` |
| Use existing clipboard image | Click **Use Clipboard Image** or press `Ctrl+Alt+V` |
| Open image file | Click **Open Image...** or drag-and-drop onto the preview area |
| Regenerate | Click **Regenerate** to re-process the current image |
| Cancel processing | Click **Cancel** or press `Esc` (also cancels a pending snip) |
| Edit result | Click **Edit** to unlock the LaTeX text box; **Lock** to re-lock |
| Copy result | **Copy Result** button, or it is copied automatically after generation |

The **History** panel shows the last 25 results. Use the search box to filter by LaTeX content or source. Right-click the image preview to save it as a file.

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | Your OpenAI API key |
| `LATEX_AGENT_MODEL` | `gpt-4.1` | Model to use (also editable in the app) |
| `LATEX_AGENT_CAPTURE_HOTKEY` | `ctrl+alt+s` | Global hotkey for new screenshot |
| `LATEX_AGENT_CLIPBOARD_HOTKEY` | `ctrl+alt+v` | Global hotkey for clipboard image |
| `LATEX_AGENT_SYSTEM_PROMPT` | *(built-in)* | Override the system prompt sent to the model |

## Notes

- Results are returned without surrounding `$...$` for easier pasting into Word.
- Complex layouts (matrices, aligned systems, piecewise) are prompted to use matching LaTeX environments.
- Drag-and-drop requires `tkinterdnd2` (included in `requirements.txt`). The app works without it if the package is unavailable.
- History is stored locally in `history.json` (excluded from version control).
