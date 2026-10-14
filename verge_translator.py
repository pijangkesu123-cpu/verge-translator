import json
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

APP = "Verge Translator"
EXTENSIONS = {".ks", ".txt", ".csv"}
TOKEN = re.compile(
    r"(\[[^\]\r\n]*\]|"
    r"\{[^{}\r\n]*\}|"
    r"<[^>\r\n]*>|"
    r"\\[a-zA-Z]+(?:\[[^\]\r\n]*\])?)"
)
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

MAX_WORKERS = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5  # detik, naik eksponensial per percobaan


def translate_api(text, endpoint, api_key):
    data = {
        "q": text,
        "source": "ja",
        "target": "id",
        "format": "text",
    }
    if api_key:
        data["api_key"] = api_key

    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/translate",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read().decode("utf-8"))

    translated = result.get("translatedText")
    if not isinstance(translated, str):
        raise ValueError("Respons API tidak berisi translatedText.")
    return translated


def translate_api_with_retry(text, endpoint, api_key):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return translate_api(text, endpoint, api_key)
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, ValueError, TimeoutError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    raise RuntimeError(f"gagal setelah {MAX_RETRIES}x percobaan: {last_error}")


def extract_fragments(line):
    """Kembalikan daftar fragmen berbahasa Jepang dalam satu baris (tanpa token)."""
    fragments = []
    for piece in TOKEN.split(line):
        if not piece or TOKEN.fullmatch(piece):
            continue
        if JAPANESE.search(piece):
            fragments.append(piece)
    return fragments


def rebuild_line(line, cache):
    ending = ""
    if line.endswith("\r\n"):
        line, ending = line[:-2], "\r\n"
    elif line.endswith("\n") or line.endswith("\r"):
        line, ending = line[:-1], line[-1:]

    pieces = TOKEN.split(line)
    output = []
    for piece in pieces:
        if not piece:
            continue
        if TOKEN.fullmatch(piece) or not JAPANESE.search(piece):
            output.append(piece)
            continue
        output.append(cache.get(piece, piece))  # fallback: biarkan asli bila belum ada di cache
    return "".join(output) + ending


def read_text(path):
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("Encoding teks tidak dikenal")


def load_cache(output):
    cache_path = output / "fragment_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_cache(output, cache):
    cache_path = output / "fragment_cache.json"
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1),
        encoding="utf-8"
    )


def run_job(source, output, endpoint, api_key, log, stop_flag):
    source = Path(source).resolve()
    output = Path(output).resolve()

    if not source.is_dir():
        raise ValueError("Folder sumber tidak ditemukan.")
    if source == output or source in output.parents:
        raise ValueError("Output harus di luar folder sumber.")
    if output.exists() and output in source.parents:
        raise ValueError("Output tidak boleh berada di dalam sumber.")

    output.mkdir(parents=True, exist_ok=True)
    cache = load_cache(output)  # fragmen dari run sebelumnya dipakai ulang
    report = []
    translated_files = 0

    files = [p for p in source.rglob("*") if p.is_file()]

    # --- Tahap 1: baca semua file, kumpulkan fragmen unik yang belum ada di cache ---
    file_data = {}  # path -> (lines, encoding) untuk file yang perlu diproses
    pending_fragments = set()

    for path in files:
        if output == path.resolve() or output in path.resolve().parents:
            continue
        if path.suffix.lower() not in EXTENSIONS:
            continue
        try:
            original, encoding = read_text(path)
        except (OSError, ValueError) as exc:
            report.append(f"GAGAL BACA: {path.relative_to(source)}: {exc}")
            continue

        lines = original.splitlines(keepends=True)
        file_data[path] = (lines, encoding)
        for line in lines:
            for fragment in extract_fragments(line):
                if fragment not in cache:
                    pending_fragments.add(fragment)

    log(f"Fragmen unik yang perlu diterjemahkan: {len(pending_fragments)}")

    # --- Tahap 2: terjemahkan fragmen unik secara paralel ---
    pending_list = list(pending_fragments)
    done_count = 0
    cache_lock = threading.Lock()

    def worker(fragment):
        return fragment, translate_api_with_retry(fragment, endpoint, api_key)

    if pending_list:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(worker, f): f for f in pending_list}
            for future in as_completed(futures):
                if stop_flag.is_set():
                    break
                fragment = futures[future]
                try:
                    _, translated = future.result()
                    with cache_lock:
                        cache[fragment] = translated
                except Exception as exc:
                    report.append(f"GAGAL FRAGMEN: {fragment!r}: {exc}")
                done_count += 1
                if done_count % 10 == 0 or done_count == len(pending_list):
                    log(f"Fragmen diterjemahkan: {done_count}/{len(pending_list)}")

    save_cache(output, cache)

    # --- Tahap 3: tulis ulang setiap file menggunakan cache ---
    for index, path in enumerate(files, 1):
        rel = path.relative_to(source)
        dest = output / rel

        if output == path.resolve() or output in path.resolve().parents:
            continue

        if path.suffix.lower() not in EXTENSIONS:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            report.append(f"DISALIN: {rel}")
            log(f"{index}/{len(files)}: {rel}")
            continue

        if path not in file_data:
            # Gagal dibaca di tahap 1; sudah tercatat di report.
            continue

        lines, encoding = file_data[path]
        translated = [rebuild_line(line, cache) for line in lines]
        new_content = "".join(translated)

        dest.parent.mkdir(parents=True, exist_ok=True)
        if new_content != "".join(lines):
            dest.write_bytes(new_content.encode(encoding))
            translated_files += 1
            report.append(f"DIPROSES: {rel}")
        else:
            shutil.copy2(path, dest)
            report.append(f"TANPA PERUBAHAN: {rel}")

        log(f"{index}/{len(files)}: {rel}")

    (output / "translation_report.txt").write_text(
        "\n".join(report)
        + f"\n\nBerkas berubah: {translated_files}"
        + f"\nFragmen unik total di cache: {len(cache)}\n",
        encoding="utf-8"
    )
    return translated_files, len(cache)


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP)
        root.geometry("650x520")
        root.minsize(520, 420)

        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.endpoint = tk.StringVar(value="https://libretranslate.com")
        self.api_key = tk.StringVar()
        self.status = tk.StringVar(value="Siap.")
        self.stop_flag = threading.Event()

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=APP, font=("Arial", 18, "bold")).pack(anchor="w")
        ttk.Label(
            frame, text="Jepang → Indonesia | KiriKiri / LiveMaker"
        ).pack(anchor="w", pady=(0, 12))

        self.folder_row(frame, "Folder sumber", self.source)
        self.folder_row(frame, "Folder output", self.output)
        self.entry_row(frame, "API endpoint", self.endpoint)
        self.entry_row(frame, "API key (opsional)", self.api_key, show="*")

        self.btn_start = ttk.Button(
            frame, text="Mulai terjemahkan", command=self.start
        )
        self.btn_start.pack(fill="x", pady=12)

        ttk.Label(frame, textvariable=self.status, wraplength=590).pack(anchor="w")

        ttk.Label(
            frame,
            text=(
                "Hanya berkas teks .ks, .txt, .csv yang diproses. Arsip "
                "XP3 dan data biner lainnya disalin tanpa modifikasi. "
                "Fragmen yang sudah diterjemahkan disimpan di "
                "fragment_cache.json dan dipakai ulang pada proses "
                "berikutnya."
            ),
            wraplength=590
        ).pack(anchor="w", pady=12)

    def folder_row(self, parent, label, var):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=17).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(
            row, text="Pilih",
            command=lambda: var.set(filedialog.askdirectory() or var.get())
        ).pack(side="right")

    def entry_row(self, parent, label, var, show=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=17).pack(side="left")
        ttk.Entry(row, textvariable=var, show=show).pack(side="left", fill="x", expand=True, padx=5)

    def start(self):
        source = self.source.get().strip()
        output = self.output.get().strip()
        endpoint = self.endpoint.get().strip()
        key = self.api_key.get().strip()

        if not source or not output or not endpoint:
            messagebox.showerror(APP, "Isi folder sumber, output, dan endpoint.")
            return

        if not endpoint.startswith("https://"):
            messagebox.showerror(APP, "Gunakan endpoint HTTPS.")
            return

        self.stop_flag.clear()
        self.btn_start.config(state="disabled", text="Memproses...")
        self.status.set("Memulai pekerjaan...")
        threading.Thread(
            target=self.worker,
            args=(source, output, endpoint, key),
            daemon=True
        ).start()

    def worker(self, source, output, endpoint, key):
        def log(message):
            self.root.after(0, lambda m=message: self.status.set(m))

        try:
            files, fragments = run_job(
                source, output, endpoint, key, log, self.stop_flag
            )
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    APP,
                    f"Selesai.\nBerkas berubah: {files}"
                    f"\nFragmen unik di cache: {fragments}"
                    "\nLihat translation_report.txt."
                )
            )
            log("Selesai.")
        except Exception as exc:
            log("Pekerjaan gagal.")
            self.root.after(0, lambda e=str(exc): messagebox.showerror(APP, e))
        finally:
            self.root.after(
                0,
                lambda: self.btn_start.config(
                    state="normal", text="Mulai terjemahkan"
                )
            )


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
