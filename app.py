import base64
import hashlib
import io
import json
import os
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import keyboard
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageGrab, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

try:
    from pix2tex.cli import LatexOCR as _Pix2TexOCR
    _PIX2TEX_AVAILABLE = True
except ImportError:
    _PIX2TEX_AVAILABLE = False

DEFAULT_SYSTEM_PROMPT = """You extract mathematical expressions from screenshots.
Return only valid LaTeX for the equation content.
Do not include markdown fences.
Do not include surrounding dollar signs.
Do not explain anything.
If the image contains multiple lines that belong together, return a suitable LaTeX environment such as aligned, cases, bmatrix, pmatrix, or similar.
Preserve mathematical meaning over visual style."""

CLIPBOARD_POLL_MS = 800
PREVIEW_SIZE = (640, 240)
HISTORY_LIMIT = 25
DEFAULT_CAPTURE_HOTKEY = "ctrl+alt+s"
DEFAULT_CLIPBOARD_HOTKEY = "ctrl+alt+v"
HISTORY_FILE = Path(__file__).with_name("history.json")
MODEL_PRESETS = ["gpt-4.1", "gpt-4o", "gpt-4-turbo", "gpt-4o-mini"]
API_TIMEOUT = 30
BACKENDS = ["OpenAI", "pix2tex (local)"]

_BaseClass = TkinterDnD.Tk if _DND_AVAILABLE else tk.Tk


class LatexAgentApp(_BaseClass):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv()

        self.title("Screenshot to LaTeX")
        self.geometry("1180x780")
        self.minsize(1020, 700)

        self._api_key: str | None = None
        self.client: OpenAI | None = None
        self._pix2tex_model = None
        self.current_image: Image.Image | None = None
        self.preview_image: ImageTk.PhotoImage | None = None
        self.waiting_for_snip = False
        self.last_clipboard_signature: str | None = None
        self.busy = False
        self._cancel_requested = False
        self.default_model = os.getenv("LATEX_AGENT_MODEL", "gpt-4.1")
        self.capture_hotkey = os.getenv("LATEX_AGENT_CAPTURE_HOTKEY", DEFAULT_CAPTURE_HOTKEY)
        self.clipboard_hotkey = os.getenv("LATEX_AGENT_CLIPBOARD_HOTKEY", DEFAULT_CLIPBOARD_HOTKEY)
        self.system_prompt = os.getenv("LATEX_AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        self.model_var = tk.StringVar(value=self.default_model)
        self.backend_var = tk.StringVar(value=BACKENDS[0])
        self.status_var = tk.StringVar(value="Ready.")
        self.result_locked = True
        self._search_var = tk.StringVar()
        self._filtered_entries: list[dict] = []
        self.history_entries: list[dict] = self._load_history_entries()
        self.hotkeys_registered = False

        self._build_ui()
        self._refresh_history_list()
        self._register_hotkeys()
        self._validate_api_key_on_startup()
        self.after(CLIPBOARD_POLL_MS, self._poll_clipboard)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", self._on_escape)

    def _validate_api_key_on_startup(self) -> None:
        if self.backend_var.get() == "OpenAI" and not os.getenv("OPENAI_API_KEY", "").strip():
            self.status_var.set("Warning: OPENAI_API_KEY not set. Add it to .env and restart.")
            messagebox.showwarning(
                "API Key Missing",
                "OPENAI_API_KEY is not set.\nAdd it to your .env file and restart the app.",
            )

    def _on_backend_change(self) -> None:
        using_openai = self.backend_var.get() == "OpenAI"
        self._model_combo.configure(state="readonly" if using_openai else "disabled")
        if not using_openai and not _PIX2TEX_AVAILABLE:
            messagebox.showwarning(
                "pix2tex not installed",
                "pix2tex is not installed.\n\nRun:\n  pip install pix2tex\n\nthen restart the app.",
            )
            self.backend_var.set("OpenAI")
            self._model_combo.configure(state="readonly")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        ttk.Label(
            root,
            text="Equation Screenshot -> LaTeX -> Clipboard",
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w")
        ttk.Label(
            root,
            text=(
                "Use the Windows snipping overlay, or load an image directly. "
                "The generated LaTeX is copied to your clipboard automatically."
            ),
            wraplength=980,
        ).pack(anchor="w", pady=(6, 14))

        # Controls row
        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Button(controls, text="New Screenshot", command=self.start_snip_workflow).pack(side="left")
        ttk.Button(controls, text="Use Clipboard Image", command=self.use_clipboard_image).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Open Image...", command=self.open_image).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Regenerate", command=self.regenerate_current_image).pack(side="left", padx=(8, 0))
        self._cancel_btn = ttk.Button(controls, text="Cancel", command=self._request_cancel, state="disabled")
        self._cancel_btn.pack(side="left", padx=(8, 0))

        # Backend + model row
        model_row = ttk.Frame(root)
        model_row.pack(fill="x", pady=(0, 8))
        ttk.Label(model_row, text="Backend").pack(side="left")
        for b in BACKENDS:
            ttk.Radiobutton(
                model_row, text=b, variable=self.backend_var, value=b,
                command=self._on_backend_change,
            ).pack(side="left", padx=(6, 0))
        ttk.Separator(model_row, orient="vertical").pack(side="left", fill="y", padx=(12, 0))
        ttk.Label(model_row, text="Model").pack(side="left", padx=(12, 0))
        self._model_combo = ttk.Combobox(
            model_row, textvariable=self.model_var, values=MODEL_PRESETS, width=26
        )
        self._model_combo.pack(side="left", padx=(8, 0))
        ttk.Label(
            model_row,
            text=(
                f"Hotkeys: {self.capture_hotkey} (screenshot)  "
                f"{self.clipboard_hotkey} (clipboard)  Esc (cancel snip)"
            ),
        ).pack(side="left", padx=(14, 0))

        # Progress bar
        self._progress = ttk.Progressbar(root, mode="indeterminate", length=200)
        self._progress.pack(fill="x", pady=(0, 8))

        # Content panes
        content = ttk.Panedwindow(root, orient="horizontal")
        content.pack(fill="both", expand=True)
        main_panel = ttk.Frame(content)
        history_panel = ttk.Frame(content, width=320)
        content.add(main_panel, weight=3)
        content.add(history_panel, weight=1)

        # Image preview
        preview_frame = ttk.LabelFrame(main_panel, text="Image")
        preview_frame.pack(fill="x", expand=False)
        drop_hint = "  Drop an image file here." if _DND_AVAILABLE else ""
        self.preview_label = ttk.Label(
            preview_frame,
            text=f"No image loaded yet.{drop_hint}",
            anchor="center",
            width=90,
        )
        self.preview_label.pack(fill="both", expand=True, padx=12, pady=12)
        self.preview_label.bind("<Button-3>", self._on_preview_right_click)
        if _DND_AVAILABLE:
            self.preview_label.drop_target_register(DND_FILES)
            self.preview_label.dnd_bind("<<Drop>>", self._on_drop)

        # LaTeX result
        result_frame = ttk.LabelFrame(main_panel, text="LaTeX")
        result_frame.pack(fill="both", expand=True, pady=(12, 0))
        result_toolbar = ttk.Frame(result_frame)
        result_toolbar.pack(fill="x", padx=12, pady=(8, 0))
        self._lock_btn = ttk.Button(result_toolbar, text="Edit", command=self._toggle_result_lock)
        self._lock_btn.pack(side="left")
        self.result_text = tk.Text(result_frame, wrap="word", font=("Consolas", 12), state="disabled")
        self.result_text.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        # History panel
        history_frame = ttk.LabelFrame(history_panel, text="History")
        history_frame.pack(fill="both", expand=True)

        search_row = ttk.Frame(history_frame)
        search_row.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(search_row, text="Search:").pack(side="left")
        ttk.Entry(search_row, textvariable=self._search_var).pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._search_var.trace_add("write", lambda *_: self._refresh_history_list())

        self.history_list = tk.Listbox(history_frame, exportselection=False, height=18)
        self.history_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.history_list.bind("<<ListboxSelect>>", self._on_history_select)

        history_actions = ttk.Frame(history_frame)
        history_actions.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Button(history_actions, text="Copy", command=self.copy_selected_history).pack(side="left")
        ttk.Button(history_actions, text="Load", command=self.load_selected_history).pack(side="left", padx=(6, 0))
        ttk.Button(history_actions, text="Delete", command=self.delete_selected_history).pack(side="left", padx=(6, 0))
        ttk.Button(history_actions, text="Clear All", command=self.clear_all_history).pack(side="left", padx=(6, 0))

        details_frame = ttk.LabelFrame(history_frame, text="Selected Entry")
        details_frame.pack(fill="both", expand=False, padx=12, pady=(0, 4))
        detail_toolbar = ttk.Frame(details_frame)
        detail_toolbar.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Button(detail_toolbar, text="Copy", command=self._copy_history_detail).pack(side="left")
        self.history_detail = tk.Text(details_frame, wrap="word", height=8, font=("Consolas", 10))
        self.history_detail.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Footer
        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")
        ttk.Button(footer, text="Copy Result", command=self.copy_result).pack(side="right")

    # ---- Snip workflow ----

    def start_snip_workflow(self) -> None:
        if self.busy:
            self.status_var.set("Already processing. Cancel or wait.")
            return
        self.waiting_for_snip = True
        self.last_clipboard_signature = self._clipboard_signature(self._read_clipboard_image())
        self.status_var.set("Waiting for snip. Press Esc to cancel.")
        try:
            os.startfile("ms-screenclip:")
        except OSError:
            self.status_var.set(
                "Could not launch snipping overlay. Use Win+Shift+S then 'Use Clipboard Image'."
            )

    def _on_escape(self, _event: tk.Event) -> None:
        if self.waiting_for_snip:
            self.waiting_for_snip = False
            self.status_var.set("Snip cancelled.")

    def use_clipboard_image(self) -> None:
        image = self._read_clipboard_image()
        if image is None:
            messagebox.showerror("No image", "Clipboard does not contain an image.")
            return
        self._set_current_image(image)
        self.process_current_image(source="clipboard")

    def open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Open equation image",
            filetypes=[
                ("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        image = Image.open(path).convert("RGB")
        self._set_current_image(image)
        self.status_var.set(f"Loaded {Path(path).name}.")
        self.process_current_image(source=Path(path).name)

    def regenerate_current_image(self) -> None:
        if self.current_image is None:
            messagebox.showerror("No image", "Load or capture an image first.")
            return
        self.process_current_image(source="regenerate")

    def _on_drop(self, event) -> None:
        raw = event.data.strip()
        path = Path(raw.strip("{}"))
        if not path.exists():
            return
        try:
            image = Image.open(path).convert("RGB")
        except Exception:
            return
        self._set_current_image(image)
        self.status_var.set(f"Dropped {path.name}.")
        self.process_current_image(source=path.name)

    # ---- Processing ----

    def process_current_image(self, source: str = "manual") -> None:
        if self.current_image is None:
            messagebox.showerror("No image", "Load or capture an image first.")
            return
        if self.busy:
            return

        self.busy = True
        self._cancel_requested = False
        self._cancel_btn.configure(state="normal")
        self._progress.start(12)
        backend = self.backend_var.get()
        self.status_var.set(f"Analyzing image with {backend}...")
        self._set_result_text("")

        image_snapshot = self.current_image  # capture reference before thread starts
        worker = threading.Thread(
            target=self._generate_latex_worker, args=(source, image_snapshot), daemon=True
        )
        worker.start()

    def _request_cancel(self) -> None:
        self._cancel_requested = True
        self.status_var.set("Cancelling...")

    def _generate_latex_worker(self, source: str, image: Image.Image) -> None:
        try:
            if self.backend_var.get() == "pix2tex (local)":
                latex = self._run_pix2tex(image)
            else:
                latex = self._run_openai(image)
            if self._cancel_requested:
                self.after(0, self._handle_cancel)
                return
            if not latex:
                raise RuntimeError("The model returned an empty result.")
            self.after(0, lambda: self._handle_result(latex, source))
        except Exception as exc:  # noqa: BLE001
            if self._cancel_requested:
                self.after(0, self._handle_cancel)
            else:
                self.after(0, lambda: self._handle_error(str(exc)))

    def _run_openai(self, image: Image.Image) -> str:
        client = self._get_client()
        data_url = self._image_to_data_url(image)
        response = client.responses.create(
            model=self.model_var.get().strip() or self.default_model,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self.system_prompt}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Extract the mathematical expression from this image "
                                "and return only the LaTeX."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "high",
                        },
                    ],
                },
            ],
            timeout=API_TIMEOUT,
        )
        return response.output_text.strip()

    def _run_pix2tex(self, image: Image.Image) -> str:
        if not _PIX2TEX_AVAILABLE:
            raise RuntimeError("pix2tex is not installed. Run: pip install pix2tex")
        if self._pix2tex_model is None:
            self.after(0, lambda: self.status_var.set(
                "Loading pix2tex model (first run downloads weights, please wait)..."
            ))
            self._pix2tex_model = _Pix2TexOCR()
        return self._pix2tex_model(image)

    def _handle_result(self, latex: str, source: str) -> None:
        self._end_busy()
        self._set_result_text(latex)
        self.copy_result()
        self._add_history_entry(latex, source)
        self.status_var.set("LaTeX generated and copied to the clipboard.")

    def _handle_cancel(self) -> None:
        self._end_busy()
        self.status_var.set("Cancelled.")

    def _handle_error(self, message: str) -> None:
        self._end_busy()
        self.status_var.set("Generation failed.")
        messagebox.showerror("Generation failed", message)

    def _end_busy(self) -> None:
        self.busy = False
        self._cancel_requested = False
        self._cancel_btn.configure(state="disabled")
        self._progress.stop()

    # ---- Result text ----

    def _set_result_text(self, text: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        if text:
            self.result_text.insert("1.0", text)
        if self.result_locked:
            self.result_text.configure(state="disabled")

    def _toggle_result_lock(self) -> None:
        self.result_locked = not self.result_locked
        self.result_text.configure(state="disabled" if self.result_locked else "normal")
        self._lock_btn.configure(text="Edit" if self.result_locked else "Lock")

    # ---- Copy / clipboard ----

    def copy_result(self) -> None:
        self.result_text.configure(state="normal")
        latex = self.result_text.get("1.0", "end").strip()
        if self.result_locked:
            self.result_text.configure(state="disabled")
        if not latex:
            return
        self._copy_text_to_clipboard(latex)

    def copy_selected_history(self) -> None:
        entry = self._selected_history_entry()
        if entry is None:
            return
        self._copy_text_to_clipboard(entry["latex"])
        self.status_var.set("Copied to clipboard.")

    def _copy_history_detail(self) -> None:
        entry = self._selected_history_entry()
        if entry is None:
            return
        self._copy_text_to_clipboard(entry["latex"])
        self.status_var.set("Copied to clipboard.")

    def _copy_text_to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()

    # ---- Image preview ----

    def _set_current_image(self, image: Image.Image) -> None:
        self.current_image = image.convert("RGB")
        preview = self.current_image.copy()
        preview.thumbnail(PREVIEW_SIZE)
        self.preview_image = ImageTk.PhotoImage(preview)
        self.preview_label.configure(image=self.preview_image, text="")

    def _on_preview_right_click(self, event: tk.Event) -> None:
        if self.current_image is None:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Save image as...", command=self._save_current_image)
        menu.tk_popup(event.x_root, event.y_root)

    def _save_current_image(self) -> None:
        if self.current_image is None:
            return
        path = filedialog.asksaveasfilename(
            title="Save image",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("All files", "*.*")],
        )
        if not path:
            return
        self.current_image.save(path)
        self.status_var.set(f"Image saved to {Path(path).name}.")

    # ---- Clipboard polling ----

    def _poll_clipboard(self) -> None:
        try:
            if self.waiting_for_snip and not self.busy:
                image = self._read_clipboard_image()  # read once
                current_sig = self._clipboard_signature(image)
                if current_sig is not None and current_sig != self.last_clipboard_signature:
                    self.waiting_for_snip = False
                    self._set_current_image(image)
                    self.status_var.set("New screenshot detected. Generating LaTeX...")
                    self.process_current_image(source="screenshot")
        finally:
            self.after(CLIPBOARD_POLL_MS, self._poll_clipboard)

    def _clipboard_signature(self, image: Image.Image | None) -> str | None:
        if image is None:
            return None
        return hashlib.md5(image.tobytes()).hexdigest()

    def _read_clipboard_image(self) -> Image.Image | None:
        data = ImageGrab.grabclipboard()
        if isinstance(data, Image.Image):
            return data.convert("RGB")
        if isinstance(data, list) and data:
            first_path = Path(data[0])
            if first_path.exists():
                return Image.open(first_path).convert("RGB")
        return None

    # ---- OpenAI client ----

    def _get_client(self) -> OpenAI:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env and restart.")
        if self.client is None or api_key != self._api_key:
            self.client = OpenAI(api_key=api_key)
            self._api_key = api_key
        return self.client

    # ---- Hotkeys ----

    def _register_hotkeys(self) -> None:
        try:
            keyboard.add_hotkey(self.capture_hotkey, self._hotkey_new_screenshot)
            keyboard.add_hotkey(self.clipboard_hotkey, self._hotkey_clipboard_image)
            self.hotkeys_registered = True
        except Exception as exc:  # noqa: BLE001
            self.hotkeys_registered = False
            self.status_var.set(f"Hotkeys unavailable: {exc}")

    def _hotkey_new_screenshot(self) -> None:
        self.after(0, self.start_snip_workflow)

    def _hotkey_clipboard_image(self) -> None:
        self.after(0, self.use_clipboard_image)

    # ---- History ----

    def _add_history_entry(self, latex: str, source: str) -> None:
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "latex": latex,
        }
        self.history_entries.insert(0, entry)
        self.history_entries = self.history_entries[:HISTORY_LIMIT]
        self._save_history_entries()
        self._refresh_history_list(select_index=0)

    def _load_history_entries(self) -> list[dict]:
        if not HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [
            item for item in data
            if isinstance(item, dict)
            and isinstance(item.get("timestamp"), str)
            and isinstance(item.get("source"), str)
            and isinstance(item.get("latex"), str)
        ][:HISTORY_LIMIT]

    def _save_history_entries(self) -> None:
        HISTORY_FILE.write_text(
            json.dumps(self.history_entries, indent=2), encoding="utf-8"
        )

    def _refresh_history_list(self, select_index: int | None = None) -> None:
        query = self._search_var.get().lower()
        self._filtered_entries = [
            e for e in self.history_entries
            if not query or query in e["latex"].lower() or query in e["source"].lower()
        ]
        self.history_list.delete(0, "end")
        for entry in self._filtered_entries:
            preview = entry["latex"].replace("\n", " ")
            if len(preview) > 42:
                preview = preview[:39] + "..."
            self.history_list.insert("end", f"{entry['timestamp']} | {preview}")
        if select_index is not None and self._filtered_entries:
            self.history_list.selection_clear(0, "end")
            self.history_list.selection_set(select_index)
            self.history_list.activate(select_index)
            self._set_history_detail(self._filtered_entries[select_index])

    def _selected_history_entry(self) -> dict | None:
        selection = self.history_list.curselection()
        if not selection:
            self.status_var.set("Select a history entry first.")
            return None
        return self._filtered_entries[selection[0]]

    def _on_history_select(self, _event: tk.Event) -> None:
        entry = self._selected_history_entry()
        if entry is not None:
            self._set_history_detail(entry)

    def _set_history_detail(self, entry: dict) -> None:
        self.history_detail.delete("1.0", "end")
        self.history_detail.insert(
            "1.0",
            f"Time: {entry['timestamp']}\nSource: {entry['source']}\n\n{entry['latex']}",
        )

    def load_selected_history(self) -> None:
        entry = self._selected_history_entry()
        if entry is None:
            return
        self._set_result_text(entry["latex"])
        self._set_history_detail(entry)
        self.status_var.set("Loaded into editor.")

    def delete_selected_history(self) -> None:
        selection = self.history_list.curselection()
        if not selection:
            return
        entry = self._filtered_entries[selection[0]]
        self.history_entries = [e for e in self.history_entries if e is not entry]
        self._save_history_entries()
        self._refresh_history_list()
        self.history_detail.delete("1.0", "end")
        self.status_var.set("Entry deleted.")

    def clear_all_history(self) -> None:
        if not self.history_entries:
            return
        if not messagebox.askyesno("Clear History", "Delete all history entries?"):
            return
        self.history_entries.clear()
        self._save_history_entries()
        self._refresh_history_list()
        self.history_detail.delete("1.0", "end")
        self.status_var.set("History cleared.")

    # ---- Image utility ----

    @staticmethod
    def _image_to_data_url(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    # ---- Close ----

    def _on_close(self) -> None:
        if self.hotkeys_registered:
            keyboard.unhook_all_hotkeys()
        self.destroy()


if __name__ == "__main__":
    app = LatexAgentApp()
    app.mainloop()
