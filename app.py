from __future__ import annotations

import queue
import logging
import logging.handlers
import json
import os
import sys
import threading
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fmp12_reader import (
    FMPError,
    TableInfo,
    build_sort_index,
    cache_is_fresh,
    cache_path_for,
    convert_legacy_sqlite_cache,
    export_to_json_cache,
    legacy_cache_is_fresh,
    legacy_cache_path_for,
    load_catalogue,
    read_json_page,
    read_search_page,
    read_sorted_json_page,
    search_json_table,
    sort_index_is_fresh,
    sort_index_path_for,
)


APP_TITLE = "FileMaker Viewer"
PAGE_SIZES = ("50", "100", "250")
RECENT_LIMIT = 10
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FMP12Viewer" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "viewer.log"
RECENT_FILES_PATH = LOG_DIR.parent / "recent_files.json"
LOGGER = logging.getLogger("fmp12viewer")
if not LOGGER.handlers:
    log_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    LOGGER.addHandler(log_handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.info("Avvio applicazione.")
DEFAULT_DATABASES = Path.home() / "Documents"
SUPPORTED_EXTENSIONS = {".fp3", ".fp5", ".fp7", ".fmp12"}


class FMP12Viewer:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(760, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.path_var = tk.StringVar()
        self.recent_files = self._load_recent_files()
        self.search_var = tk.StringVar()
        self.search_status_var = tk.StringVar(value="Inserisci almeno 3 caratteri")
        self.status_var = tk.StringVar(value="Seleziona un file FileMaker.")
        self.events: queue.Queue[tuple] = queue.Queue()
        self.search_events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.search_cancel_event: threading.Event | None = None
        self.search_after_id: str | None = None
        self.search_generation = 0
        self.active_search: dict | None = None
        self.progress_window: tk.Toplevel | None = None
        self.progress_text: tk.StringVar | None = None
        self.progress_bar: ttk.Progressbar | None = None
        self.cancel_button: ttk.Button | None = None
        self.database_path: Path | None = None
        self.source_format = ""
        self.tables: list[TableInfo] = []
        self.tabs: list[dict] = []
        self.pending_sort: dict | None = None
        self.use_table_selector = False

        self._build_ui()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(toolbar, text="Database FileMaker").grid(row=0, column=0, padx=(0, 8))
        self.path_entry = ttk.Combobox(
            toolbar,
            textvariable=self.path_var,
            values=self.recent_files,
            height=RECENT_LIMIT,
            state="normal",
        )
        self.path_entry.grid(row=0, column=1, sticky="ew")
        self.path_entry.bind("<<ComboboxSelected>>", self._open_selected_recent)
        self.browse_button = ttk.Button(
            toolbar, text="Sfoglia…", command=self.browse_file
        )
        self.browse_button.grid(row=0, column=2, padx=(8, 8))
        self.structure_button = ttk.Button(
            toolbar, text="Struttura", command=self.show_structure
        )
        self.structure_button.grid(row=0, column=3, padx=(0, 8))
        self.log_button = ttk.Button(toolbar, text="Log", command=self.show_logs)
        self.log_button.grid(row=0, column=4, padx=(0, 8))
        self.open_button = ttk.Button(
            toolbar, text="Apri", command=self.open_file, default="active"
        )
        self.open_button.grid(row=0, column=5, sticky="e")
        self.root.bind("<Return>", lambda _event: self.open_file())

        search_bar = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        search_bar.grid(row=1, column=0, sticky="ew")
        search_bar.columnconfigure(1, weight=1)
        ttk.Label(search_bar, text="Cerca in tutti i record").grid(
            row=0, column=0, padx=(0, 8)
        )
        self.search_entry = ttk.Entry(search_bar, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_entry.bind("<Return>", self._search_enter)
        ttk.Button(search_bar, text="Pulisci", command=self._clear_search).grid(
            row=0, column=2, padx=(6, 10)
        )
        ttk.Label(
            search_bar,
            textvariable=self.search_status_var,
            width=30,
            anchor="w",
        ).grid(row=0, column=3, sticky="ew")
        self.search_var.trace_add("write", self._search_text_changed)

        self.table_area = ttk.Frame(self.root)
        self.table_area.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 4))
        self.table_area.columnconfigure(0, weight=1)
        self.table_area.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self.table_area)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook.bind("<<NotebookTabChanged>>", self._search_tab_changed)

        self.selector_view = ttk.Frame(self.table_area)
        self.selector_view.grid(row=0, column=0, sticky="nsew")
        self.selector_view.columnconfigure(0, weight=1)
        self.selector_view.rowconfigure(1, weight=1)
        selector_toolbar = ttk.Frame(self.selector_view, padding=(0, 0, 0, 6))
        selector_toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(selector_toolbar, text="Tabella").grid(row=0, column=0, padx=(0, 8))
        self.table_selector = ttk.Combobox(
            selector_toolbar, state="readonly", width=48, height=15
        )
        self.table_selector.grid(row=0, column=1, sticky="ew")
        selector_toolbar.columnconfigure(1, weight=1)
        self.table_selector.bind(
            "<<ComboboxSelected>>", self._selector_table_changed
        )
        self.selector_content = ttk.Frame(self.selector_view)
        self.selector_content.grid(row=1, column=0, sticky="nsew")
        self.selector_content.columnconfigure(0, weight=1)
        self.selector_content.rowconfigure(0, weight=1)
        self.selector_view.grid_remove()

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(10, 6),
            relief="sunken",
        )
        status.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 10))
        self.root.after(100, self._poll_search_events)

    @staticmethod
    def _load_recent_files() -> list[str]:
        try:
            entries = json.loads(RECENT_FILES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(entries, list):
            return []
        recent: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            key = entry.casefold()
            if key in seen:
                continue
            recent.append(entry)
            seen.add(key)
            if len(recent) >= RECENT_LIMIT:
                break
        return recent

    def _remember_recent_file(self, source: Path) -> None:
        try:
            path = str(source.resolve())
            self.recent_files = [
                existing
                for existing in self.recent_files
                if existing.casefold() != path.casefold()
            ]
            self.recent_files.insert(0, path)
            self.recent_files = self.recent_files[:RECENT_LIMIT]
            self.path_entry.configure(values=self.recent_files)
            temporary = RECENT_FILES_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.recent_files, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, RECENT_FILES_PATH)
            LOGGER.info("Cronologia aggiornata: %s file recenti.", len(self.recent_files))
        except OSError:
            LOGGER.exception("Impossibile salvare la cronologia dei file recenti.")

    def _open_selected_recent(self, _event: tk.Event) -> None:
        if self.path_var.get().strip():
            self.root.after_idle(self.open_file)

    def _current_table_state(self) -> dict | None:
        if self.use_table_selector:
            index = self.table_selector.current()
            return self.tabs[index] if 0 <= index < len(self.tabs) else None
        selected = self.notebook.select()
        if not selected:
            return None
        index = self.notebook.index(selected)
        return self.tabs[index] if 0 <= index < len(self.tabs) else None

    def _selector_table_changed(self, _event: tk.Event) -> None:
        index = self.table_selector.current()
        if 0 <= index < len(self.tabs):
            self.tabs[index]["frame"].tkraise()
            self._search_tab_changed()

    def _search_enter(self, _event: tk.Event) -> str:
        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
            self.search_after_id = None
        self._start_search(self._current_table_state())
        return "break"

    def _clear_search(self) -> None:
        self.search_entry.focus_set()
        self.search_var.set("")

    def _cancel_active_search(self) -> None:
        self.search_generation += 1
        if self.search_cancel_event is not None:
            self.search_cancel_event.set()
        self.search_cancel_event = None
        self.active_search = None

    def _schedule_search(self, state: dict, delay_ms: int = 400) -> None:
        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
        self.search_after_id = self.root.after(
            delay_ms, lambda selected_state=state: self._start_search(selected_state)
        )

    def _reset_table_search(self, state: dict) -> None:
        result_path = state.get("search_result_path")
        if result_path is not None:
            try:
                Path(result_path).unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("Impossibile eliminare l'indice temporaneo di ricerca.")
        state["search_result_path"] = None
        state["search_match_count"] = None
        state["search_query"] = None
        saved_sort = state.get("search_saved_sort")
        if saved_sort is not None:
            state["sort_column"], state["sort_descending"] = saved_sort
            state["search_saved_sort"] = None
            self._update_sort_headings(state)
        state["page_number"].set(1)
        self._restore_search_controls(state)

    def _clear_search_filters(self) -> None:
        for state in self.tabs:
            self._reset_table_search(state)
        current = self._current_table_state() if self.tabs else None
        if current is not None and self.database_path is not None:
            self._fetch_page(current)

    def _search_text_changed(self, *_args: object) -> None:
        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
            self.search_after_id = None
        self._cancel_active_search()
        self._clear_search_filters()

        query = self.search_var.get().strip()
        if len(query) < 3:
            self.search_status_var.set("Inserisci almeno 3 caratteri")
            return
        state = self._current_table_state() if self.tabs else None
        if self.database_path is None or state is None:
            self.search_status_var.set("Apri prima un database")
            return
        state["tree"].delete(*state["tree"].get_children())
        state["page_label"].set("Ricerca in attesa…")
        state["previous"].configure(state="disabled")
        state["following"].configure(state="disabled")
        state["page_size_box"].configure(state="disabled")
        self.search_status_var.set("Ricerca in attesa…")
        self._schedule_search(state)

    def _search_tab_changed(self, _event: tk.Event | None = None) -> None:
        if not self.tabs:
            return
        state = self._current_table_state()
        query = self.search_var.get().strip()
        if state is None or len(query) < 3:
            return
        if self.active_search is not None:
            if self.active_search["state"] is state:
                return
            self._cancel_active_search()
        result_path = state.get("search_result_path")
        if (
            state.get("search_query") == query
            and result_path is not None
            and Path(result_path).is_file()
        ):
            self.search_status_var.set(
                f"{state['search_match_count']:,} risultati"
            )
            self._fetch_page(state)
            return
        state["tree"].delete(*state["tree"].get_children())
        state["page_label"].set("Ricerca in attesa…")
        state["previous"].configure(state="disabled")
        state["following"].configure(state="disabled")
        state["page_size_box"].configure(state="disabled")
        self.search_status_var.set("Ricerca in attesa…")
        self._schedule_search(state, 150)

    def _start_search(self, state: dict | None) -> None:
        self.search_after_id = None
        query = self.search_var.get().strip()
        if len(query) < 3:
            self.search_status_var.set("Inserisci almeno 3 caratteri")
            return
        if self.database_path is None or state is None:
            self.search_status_var.set("Apri prima un database")
            return
        if (
            self.active_search is not None
            and self.active_search["state"] is state
            and self.active_search["query"] == query
        ):
            return
        if self.active_search is not None:
            self._cancel_active_search()

        result_path = state.get("search_result_path")
        if (
            state.get("search_query") == query
            and result_path is not None
            and Path(result_path).is_file()
        ):
            self.search_status_var.set(f"{state['search_match_count']:,} risultati")
            self._fetch_page(state)
            return

        if state.get("search_saved_sort") is None:
            state["search_saved_sort"] = (
                state["sort_column"], state["sort_descending"]
            )
        state["sort_column"] = None
        state["sort_descending"] = False
        self._update_sort_headings(state)
        state["search_query"] = query
        state["search_result_path"] = None
        state["search_match_count"] = None
        state["page_number"].set(1)
        state["tree"].delete(*state["tree"].get_children())
        state["page_label"].set("Ricerca in corso…")
        state["previous"].configure(state="disabled")
        state["following"].configure(state="disabled")
        state["page_size_box"].configure(state="disabled")
        self.search_status_var.set("Ricerca in corso…")

        self.search_cancel_event = threading.Event()
        token = self.search_generation
        self.active_search = {"token": token, "state": state, "query": query}
        worker = threading.Thread(
            target=self._search_worker,
            args=(self.database_path, state["table"], query, token, self.search_cancel_event),
            daemon=True,
            name="fmp12-table-search",
        )
        worker.start()

    def _search_worker(
        self,
        manifest: Path,
        table: TableInfo,
        query: str,
        token: int,
        cancel: threading.Event,
    ) -> None:
        try:
            def progress(_phase: str, done: int, total: int) -> None:
                self.search_events.put(("progress", token, done, total))

            result_path, match_count = search_json_table(
                manifest, table, query, progress=progress, cancel=cancel
            )
            self.search_events.put(("done", token, result_path, match_count))
        except FMPError as exc:
            self.search_events.put(("error", token, str(exc)))
        except Exception as exc:
            LOGGER.exception("Errore inatteso durante la ricerca.")
            self.search_events.put(("error", token, f"Errore inatteso: {exc}"))

    def _poll_search_events(self) -> None:
        try:
            while True:
                event = self.search_events.get_nowait()
                kind, token = event[0], event[1]
                if (
                    self.active_search is None
                    or token != self.search_generation
                    or self.active_search["token"] != token
                ):
                    if kind == "done":
                        Path(event[2]).unlink(missing_ok=True)
                    continue
                if kind == "progress":
                    self.search_status_var.set(
                        f"Ricerca: {event[2]:,} / {event[3]:,} record"
                    )
                elif kind == "done":
                    active = self.active_search
                    state = active["state"]
                    state["search_result_path"] = Path(event[2])
                    state["search_match_count"] = event[3]
                    state["search_query"] = active["query"]
                    state["page_number"].set(1)
                    self.active_search = None
                    self.search_cancel_event = None
                    self._restore_search_controls(state)
                    self.search_status_var.set(f"{event[3]:,} risultati")
                    self.status_var.set(
                        f"Ricerca completata: {event[3]:,} record corrispondenti."
                    )
                    self._fetch_page(state)
                elif kind == "error":
                    active = self.active_search
                    state = active["state"]
                    self.active_search = None
                    self.search_cancel_event = None
                    self._reset_table_search(state)
                    self._fetch_page(state)
                    if "annullat" in event[2].casefold():
                        self.search_status_var.set("Ricerca annullata")
                    else:
                        LOGGER.error("Ricerca non riuscita: %s", event[2])
                        self.search_status_var.set("Ricerca non riuscita")
                        messagebox.showerror(
                            "Errore di ricerca", event[2], parent=self.root
                        )
        except queue.Empty:
            pass
        try:
            self.root.after(200, self._poll_search_events)
        except tk.TclError:
            pass

    def _restore_search_controls(self, state: dict) -> None:
        state["page_size_box"].configure(state="readonly")
        page = state["page_number"].get()
        total_pages = self._page_count(state)
        visible_count = state["search_match_count"]
        if visible_count is None:
            visible_count = state["table"].record_count
        state["previous"].configure(state="normal" if page > 1 else "disabled")
        state["following"].configure(
            state="normal" if page < total_pages and visible_count else "disabled"
        )

    def browse_file(self) -> None:
        start_dir = DEFAULT_DATABASES if DEFAULT_DATABASES.exists() else None
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Seleziona un database FileMaker",
            initialdir=str(start_dir) if start_dir else None,
            filetypes=(
                ("Database FileMaker", "*.fp3 *.fp5 *.fp7 *.fmp12"),
                ("Tutti i file", "*.*"),
            ),
        )
        if selected:
            self.path_var.set(selected)

    def open_file(self) -> None:
        path = Path(self.path_var.get().strip().strip('"'))
        if not path.is_file():
            messagebox.showerror(
                "File non trovato",
                "Seleziona un file FileMaker esistente.",
                parent=self.root,
            )
            return
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            messagebox.showerror(
                "Formato non supportato",
                "Seleziona un file .fp3, .fp5, .fp7 o .fmp12.",
                parent=self.root,
            )
            return
        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
            self.search_after_id = None
        self._cancel_active_search()
        try:
            LOGGER.info(
                "Apertura richiesta: %s (%s byte).",
                path,
                path.stat().st_size,
            )
        except OSError:
            LOGGER.exception("Impossibile leggere i metadati del file selezionato.")

        self.open_button.configure(state="disabled")
        self.browse_button.configure(state="disabled")
        self.path_entry.configure(state="disabled")
        self.status_var.set(f"Preparazione di {path.name}…")

        try:
            cache = cache_path_for(path)
            if cache_is_fresh(path, cache):
                LOGGER.info("Cache valida: %s", cache)
                self._show_database(cache, path)
                self._restore_controls()
                return
            legacy_cache = legacy_cache_path_for(path)
        except OSError as exc:
            LOGGER.exception("Errore durante la preparazione della cache.")
            self._restore_controls()
            messagebox.showerror("Errore", str(exc), parent=self.root)
            return

        self.cancel_event = threading.Event()
        self._show_progress_dialog(path)
        worker = threading.Thread(
            target=self._extract_worker,
            args=(path, cache, legacy_cache, self.cancel_event),
            daemon=True,
            name="fmp12-extraction",
        )
        worker.start()
        self.root.after(100, self._poll_events)

    def _show_progress_dialog(
        self,
        source: Path,
        title: str = "Apertura database",
        message: str | None = None,
    ) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", self._request_cancel)
        dialog.columnconfigure(0, weight=1)

        body = ttk.Frame(dialog, padding=18)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text=message or f"Estrazione di {source.name}",
            font=("", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.progress_text = tk.StringVar(value="Avvio lettura…")
        ttk.Label(
            body, textvariable=self.progress_text, wraplength=460, anchor="w"
        ).grid(row=1, column=0, sticky="ew", pady=(12, 8))
        self.progress_bar = ttk.Progressbar(
            body, mode="determinate", maximum=100, length=460
        )
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.cancel_button = ttk.Button(
            body, text="Annulla", command=self._request_cancel
        )
        self.cancel_button.grid(row=3, column=0, sticky="e")

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()
        dialog.lift()
        self.progress_window = dialog

    def _request_cancel(self) -> None:
        LOGGER.info("Richiesta annullamento operazione.")
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.progress_text is not None:
            self.progress_text.set("Annullamento in corso…")
        if self.cancel_button is not None:
            self.cancel_button.configure(state="disabled")

    def _extract_worker(
        self,
        source: Path,
        cache: Path,
        legacy_cache: Path,
        cancel: threading.Event,
    ) -> None:
        try:
            if cache_is_fresh(source, cache):
                LOGGER.info("Cache valida nel thread di estrazione: %s", cache)
                self.events.put(("done", cache, source, True))
                return

            if legacy_cache_is_fresh(source, legacy_cache):
                LOGGER.info("Conversione della vecchia cache locale in JSON: %s", legacy_cache)
                last_log = {"phase": "", "percent": -10}

                def conversion_progress(phase: str, done: int, total: int) -> None:
                    self.events.put(("progress", phase, done, total))
                    percent = int(done * 100 / total) if total else 0
                    if (
                        phase != last_log["phase"]
                        or percent >= last_log["percent"] + 10
                        or (total and done == total)
                    ):
                        LOGGER.info("%s: %s/%s (%s%%).", phase, done, total, percent)
                        last_log["phase"] = phase
                        last_log["percent"] = percent

                result, _tables = convert_legacy_sqlite_cache(
                    source,
                    legacy_cache,
                    cache,
                    progress=conversion_progress,
                    cancel=cancel,
                )
                LOGGER.info("Cache JSON creata dalla cache locale esistente: %s", result)
                self.events.put(("done", result, source, False))
                return

            LOGGER.info("Inizio estrazione: %s", source)
            last_log = {"phase": "", "percent": -10}

            def progress(phase: str, done: int, total: int) -> None:
                self.events.put(("progress", phase, done, total))
                percent = int(done * 100 / total) if total else 0
                if (
                    phase != last_log["phase"]
                    or percent >= last_log["percent"] + 10
                    or (total and done == total)
                ):
                    LOGGER.info("%s: %s/%s (%s%%).", phase, done, total, percent)
                    last_log["phase"] = phase
                    last_log["percent"] = percent

            result, _tables = export_to_json_cache(
                source, cache, progress=progress, cancel=cancel
            )
            LOGGER.info("Estrazione completata: %s", result)
            self.events.put(("done", result, source, False))
        except FMPError as exc:
            LOGGER.exception("Errore del lettore FMP12.")
            self.events.put(("error", str(exc)))
        except Exception as exc:  # keep unexpected parser failures visible to the user
            LOGGER.exception("Errore inatteso durante l'estrazione.")
            self.events.put(("error", f"Errore inatteso: {exc}"))

    def _sort_worker(
        self,
        manifest: Path,
        table: TableInfo,
        column_index: int,
        cancel: threading.Event,
    ) -> None:
        try:
            LOGGER.info(
                "Creazione indice di ordinamento: tabella %s, colonna %s.",
                table.name,
                column_index,
            )
            last_log = {"phase": "", "percent": -10}

            def progress(phase: str, done: int, total: int) -> None:
                self.events.put(("progress", phase, done, total))
                percent = int(done * 100 / total) if total else 0
                if (
                    phase != last_log["phase"]
                    or percent >= last_log["percent"] + 10
                    or (total and done == total)
                ):
                    LOGGER.info("%s: %s/%s (%s%%).", phase, done, total, percent)
                    last_log["phase"] = phase
                    last_log["percent"] = percent

            index_path = build_sort_index(
                manifest,
                table,
                column_index,
                progress=progress,
                cancel=cancel,
            )
            LOGGER.info("Indice di ordinamento pronto: %s", index_path)
            self.events.put(("sort_done", index_path))
        except FMPError as exc:
            LOGGER.exception("Creazione dell'indice di ordinamento non riuscita.")
            self.events.put(("error", str(exc)))
        except Exception as exc:
            LOGGER.exception("Errore inatteso durante l'ordinamento.")
            self.events.put(("error", f"Errore inatteso: {exc}"))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self._update_progress(event[1], event[2], event[3])
                elif event[0] == "done":
                    self._finish_progress()
                    self._show_database(event[1], event[2])
                    self.status_var.set(
                        "Database pronto. "
                        + ("Aperto dalla cache locale." if event[3] else "Estrazione completata.")
                    )
                    self._restore_controls()
                    return
                elif event[0] == "sort_done":
                    self._finish_progress()
                    pending = self.pending_sort
                    self.pending_sort = None
                    if pending is not None:
                        state = pending["state"]
                        state["sort_column"] = pending["column_index"]
                        state["sort_descending"] = pending["descending"]
                        state["page_number"].set(1)
                        self._update_sort_headings(state)
                        self._restore_sort_controls(state)
                        self._fetch_page(state)
                        self.status_var.set(
                            f"Ordinamento completato: {pending['label']}."
                        )
                    self._restore_controls()
                    return
                elif event[0] == "error":
                    is_sort_error = self.pending_sort is not None
                    LOGGER.error("Operazione non riuscita: %s", event[1])
                    self._finish_progress()
                    if self.pending_sort is not None:
                        self._restore_sort_controls(self.pending_sort["state"])
                        self.pending_sort = None
                    self._restore_controls()
                    message = event[1]
                    if "annullat" in message.casefold():
                        self.status_var.set("Operazione annullata.")
                    else:
                        self.status_var.set("L'operazione non è riuscita.")
                        title = "Errore di ordinamento" if is_sort_error else "Errore di apertura"
                        messagebox.showerror(title, message, parent=self.root)
                    return
        except queue.Empty:
            pass
        if self.progress_window is not None:
            self.root.after(100, self._poll_events)

    def _update_progress(self, phase: str, done: int, total: int) -> None:
        if self.progress_text is not None:
            if total:
                percent = int(done * 100 / total)
                self.progress_text.set(f"{phase}: {done:,} di {total:,} ({percent}%)")
            else:
                self.progress_text.set(f"{phase}…")
        if self.progress_bar is not None and total:
            self.progress_bar.configure(mode="determinate", maximum=100)
            self.progress_bar["value"] = min(100, int(done * 100 / total))

    def _finish_progress(self) -> None:
        if self.progress_window is not None:
            try:
                self.progress_window.grab_release()
                self.progress_window.destroy()
            except tk.TclError:
                pass
        self.progress_window = None
        self.progress_text = None
        self.progress_bar = None
        self.cancel_button = None
        self.cancel_event = None

    def _restore_controls(self) -> None:
        self.open_button.configure(state="normal")
        self.browse_button.configure(state="normal")
        self.path_entry.configure(state="normal")

    def _restore_sort_controls(self, state: dict) -> None:
        state["previous"].configure(state="normal")
        state["following"].configure(state="normal")
        state["page_size_box"].configure(state="readonly")

    def _update_sort_headings(self, state: dict) -> None:
        tree = state["tree"]
        sort_column = state["sort_column"]
        descending = state["sort_descending"]
        for column_id, label in state["heading_labels"].items():
            text = label
            if column_id == "record_number":
                column_index = -1
            else:
                column_index = int(column_id.removeprefix("field_"))
            if sort_column == column_index:
                text += " ↓" if descending else " ↑"
            tree.heading(
                column_id,
                text=text,
                command=lambda s=state, index=column_index: self._sort_by_column(
                    s, index
                ),
            )

    def _sort_by_column(self, state: dict, column_index: int) -> None:
        if self.database_path is None or self.pending_sort is not None:
            return
        if (
            state.get("search_query") is not None
            or len(self.search_var.get().strip()) >= 3
        ):
            self.search_status_var.set("Svuota la ricerca per ordinare la tabella")
            return
        if state["sort_column"] == column_index:
            descending = not state["sort_descending"]
        else:
            descending = False
        table = state["table"]
        label = "N. record" if column_index == -1 else table.fields[column_index].name

        # Record numbers already follow source order, so ascending needs no index.
        needs_index = not (column_index == -1 and not descending)
        index_path = sort_index_path_for(
            self.database_path, table, column_index
        ) if needs_index else None
        if index_path is None or sort_index_is_fresh(
            index_path, self.database_path, table, column_index
        ):
            state["sort_column"] = column_index
            state["sort_descending"] = descending
            state["page_number"].set(1)
            self._update_sort_headings(state)
            self._fetch_page(state)
            self.status_var.set(
                f"Ordinamento per {label}: "
                f"{'decrescente' if descending else 'crescente'}."
            )
            return

        self.pending_sort = {
            "state": state,
            "column_index": column_index,
            "descending": descending,
            "label": label,
        }
        state["previous"].configure(state="disabled")
        state["following"].configure(state="disabled")
        state["page_size_box"].configure(state="disabled")
        source = Path(self.path_var.get())
        self.cancel_event = threading.Event()
        self._show_progress_dialog(
            source,
            title="Ordinamento tabella",
            message=f"Preparo l'indice per «{label}» — {table.record_count:,} record",
        )
        worker = threading.Thread(
            target=self._sort_worker,
            args=(self.database_path, table, column_index, self.cancel_event),
            daemon=True,
            name="fmp12-sort-index",
        )
        worker.start()
        self.root.after(100, self._poll_events)

    def _show_database(self, database: Path, source: Path) -> None:
        try:
            tables = load_catalogue(database)
        except (OSError, FMPError, ValueError) as exc:
            messagebox.showerror(
                "Cache non leggibile", str(exc), parent=self.root
            )
            return

        for old_state in self.tabs:
            result_path = old_state.get("search_result_path")
            if result_path is not None:
                try:
                    Path(result_path).unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("Impossibile eliminare i risultati di ricerca temporanei.")
        for tab_id in self.notebook.tabs():
            tab_widget = self.notebook.nametowidget(tab_id)
            self.notebook.forget(tab_id)
            tab_widget.destroy()
        for child in self.selector_content.winfo_children():
            child.destroy()
        self.tabs.clear()
        self.database_path = database
        self.tables = tables
        try:
            self.source_format = json.loads(
                database.read_text(encoding="utf-8")
            ).get("source_format", source.suffix.lstrip(".").upper())
        except (OSError, ValueError, AttributeError):
            self.source_format = source.suffix.lstrip(".").upper()
        self.structure_button.configure(state="normal")
        self.use_table_selector = len(tables) > 10
        if self.use_table_selector:
            self.notebook.grid_remove()
            self.selector_view.grid()
            self.table_selector.configure(
                values=[f"{table.name} ({table.record_count:,})" for table in tables]
            )
            self.table_selector.current(0 if tables else -1)
        else:
            self.selector_view.grid_remove()
            self.notebook.grid()
            self.table_selector.configure(values=())
        self.path_var.set(str(source))
        self._remember_recent_file(source)

        total_records = 0
        for table in tables:
            self._add_table_tab(table)
            total_records += table.record_count
        if self.use_table_selector and self.tabs:
            self.tabs[0]["frame"].tkraise()
        self.status_var.set(
            f"{source.name} — {len(tables)} tabelle, "
            f"{total_records:,} record. Cache locale: {database}"
        )
        LOGGER.info(
            "Database visualizzato: %s; %s tabelle; %s record.",
            source,
            len(tables),
            total_records,
        )
        if len(self.search_var.get().strip()) >= 3:
            state = self._current_table_state()
            if state is not None:
                self.search_status_var.set("Ricerca in attesa…")
                self._schedule_search(state, 150)

    def _add_table_tab(self, table: TableInfo) -> None:
        parent = self.selector_content if self.use_table_selector else self.notebook
        frame = ttk.Frame(parent, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        controls.columnconfigure(3, weight=1)
        page_size = tk.StringVar(value="100")
        page_label = tk.StringVar(value="")
        page_number = tk.IntVar(value=1)

        columns = [f"field_{index}" for index, _ in enumerate(table.fields)]
        headings = [field.name for field in table.fields]
        tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=1, column=0, sticky="nsew")
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll.grid(row=2, column=0, sticky="ew")

        tree["columns"] = ["record_number"] + columns
        tree.heading("record_number", text="N. record")
        tree.column("record_number", width=90, minwidth=70, stretch=False, anchor="e")
        for column_id, heading in zip(columns, headings):
            tree.heading(column_id, text=heading)
            tree.column(column_id, width=170, minwidth=70, stretch=False, anchor="w")

        state = {
            "frame": frame,
            "table": table,
            "tree": tree,
            "page_size": page_size,
            "page_label": page_label,
            "page_number": page_number,
            "page_rows": {},
            "sort_column": None,
            "sort_descending": False,
            "search_query": None,
            "search_result_path": None,
            "search_match_count": None,
            "search_saved_sort": None,
            "heading_labels": {
                "record_number": "N. record",
                **dict(zip(columns, headings)),
            },
        }
        self._update_sort_headings(state)

        previous = ttk.Button(
            controls,
            text="← Precedente",
            command=lambda s=state: self._change_page(s, -1),
        )
        previous.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(controls, textvariable=page_label, width=18, anchor="center").grid(
            row=0, column=1, padx=4
        )
        following = ttk.Button(
            controls,
            text="Successiva →",
            command=lambda s=state: self._change_page(s, 1),
        )
        following.grid(row=0, column=2, padx=(6, 12))
        ttk.Label(controls, text="Record per pagina").grid(row=0, column=3, sticky="e")
        page_size_box = ttk.Combobox(
            controls,
            textvariable=page_size,
            values=PAGE_SIZES,
            width=6,
            state="readonly",
        )
        page_size_box.grid(row=0, column=4, padx=(6, 0))
        state["page_size_box"] = page_size_box
        page_size_box.bind(
            "<<ComboboxSelected>>",
            lambda _event, s=state: self._set_page_size(s),
        )
        state["previous"] = previous
        state["following"] = following
        tree.bind("<Double-1>", lambda _event, s=state: self._show_record_detail(s))

        tab_title = f"{table.name} ({table.record_count:,})"
        if self.use_table_selector:
            frame.grid(row=0, column=0, sticky="nsew")
        else:
            self.notebook.add(frame, text=tab_title)
        self.tabs.append(state)
        self._fetch_page(state)

    def _page_count(self, state: dict) -> int:
        page_size = max(1, int(state["page_size"].get()))
        count = state["search_match_count"]
        if count is None:
            count = state["table"].record_count
        return max(1, (count + page_size - 1) // page_size)

    def _change_page(self, state: dict, delta: int) -> None:
        new_page = state["page_number"].get() + delta
        state["page_number"].set(max(1, min(self._page_count(state), new_page)))
        self._fetch_page(state)

    def _set_page_size(self, state: dict) -> None:
        state["page_number"].set(1)
        self._fetch_page(state)

    def _fetch_page(self, state: dict) -> None:
        if self.database_path is None:
            return
        table = state["table"]
        tree = state["tree"]
        tree.delete(*tree.get_children())

        page_size = max(1, int(state["page_size"].get()))
        page = state["page_number"].get()
        total_pages = self._page_count(state)
        offset = (page - 1) * page_size
        visible_count = state["search_match_count"]
        if visible_count is None:
            visible_count = table.record_count
            record_label = f"{visible_count:,} record"
        else:
            record_label = f"{visible_count:,} risultati"
        state["page_label"].set(
            f"Pagina {page} di {total_pages} · {record_label}"
        )
        state["previous"].configure(state="normal" if page > 1 else "disabled")
        state["following"].configure(
            state="normal" if page < total_pages and visible_count else "disabled"
        )
        try:
            result_path = state.get("search_result_path")
            sort_column = state["sort_column"]
            descending = state["sort_descending"]
            if result_path is not None:
                rows = read_search_page(
                    self.database_path, table, result_path, offset, page_size
                )
            elif sort_column is None or (sort_column == -1 and not descending):
                rows = read_json_page(self.database_path, table, offset, page_size)
            else:
                sort_index = sort_index_path_for(
                    self.database_path, table, sort_column
                )
                if not sort_index_is_fresh(
                    sort_index, self.database_path, table, sort_column
                ):
                    raise FMPError("L'indice di ordinamento non è pronto.")
                rows = read_sorted_json_page(
                    self.database_path,
                    table,
                    sort_index,
                    offset,
                    page_size,
                    descending=descending,
                )
        except (OSError, ValueError, FMPError) as exc:
            LOGGER.exception("Impossibile leggere la pagina dalla cache JSON.")
            messagebox.showerror("Errore di lettura", str(exc), parent=self.root)
            return

        state["page_rows"] = {}
        for row in rows:
            record_number = row[0]
            state["page_rows"][record_number] = row
            values = [record_number]
            values.extend(self._display_value(value) for value in row[1:])
            tree.insert("", "end", iid=f"row_{record_number}", values=values)

    @staticmethod
    def _display_value(value: object) -> str:
        if value is None:
            return ""
        text = str(value)
        return text if len(text) <= 500 else text[:497] + "…"

    def _show_record_detail(self, state: dict) -> None:
        selected = state["tree"].selection()
        if not selected or self.database_path is None:
            return
        row_number = int(selected[0].removeprefix("row_"))
        table = state["table"]
        row = state["page_rows"].get(row_number)
        if row is None:
            return

        detail = tk.Toplevel(self.root)
        detail.title(f"{table.name} — record {row_number}")
        detail.transient(self.root)
        detail.geometry("760x520")
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(0, weight=1)
        text = tk.Text(detail, wrap="word", padx=12, pady=10)
        scroll = ttk.Scrollbar(detail, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        text.insert("end", f"Record {row_number}\n\n")
        for index, field_info in enumerate(table.fields, start=1):
            value = row[index]
            text.insert("end", f"{field_info.name}\n", "field")
            text.insert("end", "" if value is None else str(value))
            text.insert("end", "\n\n")
        text.tag_configure("field", font=("", 9, "bold"))
        text.configure(state="disabled")

    def close(self) -> None:
        LOGGER.info("Chiusura applicazione.")
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.search_cancel_event is not None:
            self.search_cancel_event.set()
        if self.search_after_id is not None:
            try:
                self.root.after_cancel(self.search_after_id)
            except tk.TclError:
                pass
        for state in self.tabs:
            result_path = state.get("search_result_path")
            if result_path is not None:
                try:
                    Path(result_path).unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("Impossibile eliminare i risultati di ricerca temporanei.")
        self.root.destroy()

    def show_logs(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Log dell'applicazione")
        window.transient(self.root)
        window.geometry("850x420")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        text = tk.Text(window, wrap="none", padx=8, pady=8)
        vertical = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(window, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        try:
            content = LOG_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            content = f"Impossibile leggere il log: {exc}"
        text.insert("1.0", content[-100_000:])
        text.configure(state="disabled")

    @staticmethod
    def _normalized_identifier(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value).casefold()
        return "".join(character for character in decomposed if character.isalnum())

    def _relationship_suggestions(self) -> list[tuple[str, str, str, str]]:
        suggestions: set[tuple[str, str, str, str]] = set()

        def table_names(name: str) -> set[str]:
            normalized = self._normalized_identifier(name)
            variants = {normalized}
            for suffix in ("s", "i", "e"):
                if len(normalized) > 4 and normalized.endswith(suffix):
                    variants.add(normalized[:-1])
            return variants

        for source in self.tables:
            for source_field in source.fields:
                normalized_field = self._normalized_identifier(source_field.name)
                tokens: set[str] = set()
                if normalized_field.startswith("id") and len(normalized_field) > 2:
                    tokens.add(normalized_field[2:])
                if normalized_field.endswith("id") and len(normalized_field) > 2:
                    tokens.add(normalized_field[:-2])

                for target in self.tables:
                    if target.index == source.index:
                        continue
                    target_aliases = table_names(target.name)
                    target_key_fields = [
                        field
                        for field in target.fields
                        if self._normalized_identifier(field.name) == "id"
                        or self._normalized_identifier(field.name).startswith("id")
                        or self._normalized_identifier(field.name).endswith("id")
                    ]
                    if not target_key_fields:
                        continue

                    matches_table_name = bool(tokens & target_aliases)
                    matching_shared_key = any(
                        normalized_field == self._normalized_identifier(field.name)
                        and normalized_field != "id"
                        for field in target.fields
                    )
                    if matches_table_name or matching_shared_key:
                        target_field = target_key_fields[0]
                        suggestions.add(
                            (
                                source.name,
                                source_field.name,
                                target.name,
                                target_field.name,
                            )
                        )
        return sorted(suggestions, key=lambda edge: tuple(part.casefold() for part in edge))

    def show_structure(self) -> None:
        if not self.tables:
            messagebox.showinfo(
                "Struttura database",
                "Apri prima un database FileMaker.",
                parent=self.root,
            )
            return

        window = tk.Toplevel(self.root)
        window.title(f"Struttura — {Path(self.path_var.get()).name}")
        window.transient(self.root)
        window.geometry("900x560")
        window.minsize(650, 380)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(
            window,
            text=f"Formato: {self.source_format or 'FileMaker'} — "
            f"{len(self.tables)} tabelle",
            padding=(12, 10, 12, 4),
        ).grid(row=0, column=0, sticky="w")

        notebook = ttk.Notebook(window)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        schema_frame = ttk.Frame(notebook, padding=6)
        schema_frame.columnconfigure(0, weight=1)
        schema_frame.rowconfigure(0, weight=1)
        schema_tree = ttk.Treeview(
            schema_frame,
            columns=("indice", "tipo", "campi"),
            show="tree headings",
        )
        schema_tree.heading("#0", text="Tabella / campo")
        schema_tree.heading("indice", text="Indice")
        schema_tree.heading("tipo", text="Tipo campo")
        schema_tree.heading("campi", text="Campi")
        schema_tree.column("#0", width=390, minwidth=220)
        schema_tree.column("indice", width=90, anchor="e")
        schema_tree.column("tipo", width=150)
        schema_tree.column("campi", width=90, anchor="e")
        schema_scroll = ttk.Scrollbar(
            schema_frame, orient="vertical", command=schema_tree.yview
        )
        schema_tree.configure(yscrollcommand=schema_scroll.set)
        schema_tree.grid(row=0, column=0, sticky="nsew")
        schema_scroll.grid(row=0, column=1, sticky="ns")
        for table in self.tables:
            parent = schema_tree.insert(
                "",
                "end",
                text=table.name,
                values=(table.index, "Tabella", len(table.fields)),
                open=True,
            )
            for field_info in table.fields:
                schema_tree.insert(
                    parent,
                    "end",
                    text=field_info.name,
                    values=(field_info.index, field_info.type_name, ""),
                )
        notebook.add(schema_frame, text="Tabelle e campi")

        relations_frame = ttk.Frame(notebook, padding=8)
        relations_frame.columnconfigure(0, weight=1)
        relations_frame.rowconfigure(1, weight=1)
        ttk.Label(
            relations_frame,
            text=(
                "La guida fmptools non decodifica il grafo delle relazioni di FileMaker. "
                "Questi collegamenti sono ipotesi ricavate dai nomi dei campi e vanno "
                "verificati nel file aperto con FileMaker."
            ),
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        relations_tree = ttk.Treeview(
            relations_frame,
            columns=("origine", "campo_origine", "destinazione", "campo_destinazione"),
            show="headings",
        )
        for column, heading in (
            ("origine", "Tabella origine"),
            ("campo_origine", "Campo origine"),
            ("destinazione", "Tabella collegata"),
            ("campo_destinazione", "Campo possibile chiave"),
        ):
            relations_tree.heading(column, text=heading)
            relations_tree.column(column, width=180, minwidth=120)
        relations_scroll = ttk.Scrollbar(
            relations_frame, orient="vertical", command=relations_tree.yview
        )
        relations_tree.configure(yscrollcommand=relations_scroll.set)
        relations_tree.grid(row=1, column=0, sticky="nsew")
        relations_scroll.grid(row=1, column=1, sticky="ns")
        suggestions = self._relationship_suggestions()
        for suggestion in suggestions:
            relations_tree.insert("", "end", values=suggestion)
        if not suggestions:
            relations_tree.insert(
                "",
                "end",
                values=("Nessun collegamento deducibile dai nomi", "", "", ""),
            )
        notebook.add(relations_frame, text="Collegamenti ipotizzati")

        ttk.Button(window, text="Chiudi", command=window.destroy).grid(
            row=2, column=0, sticky="e", padx=12, pady=(0, 12)
        )


def main() -> None:
    root = tk.Tk()
    app = FMP12Viewer(root)
    if len(sys.argv) > 1:
        app.path_var.set(sys.argv[1])
        root.after(200, app.open_file)
    root.mainloop()


if __name__ == "__main__":
    main()
