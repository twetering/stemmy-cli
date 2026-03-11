"""
RPG-style interactive mode for Stemmy CLI.

Menu-driven, predictable, no AI costs.
Like a classic text adventure: you navigate through proven options.
"""

import os
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

console = Console()


def get_style():
    return Style.from_dict({
        "prompt": "#ff6b6b bold",
        "": "#ffffff",
    })


class RPGShell:
    """
    Menu-driven interactive shell.
    
    No AI, no surprises. Just proven workflows.
    Navigate with numbers or shortcuts.
    """
    
    def __init__(self, debug: bool = False):
        history_path = Path.home() / ".stemmy_rpg_history"
        self.session = PromptSession(style=get_style())
        self.running = True
        self.debug = debug
        os.environ.setdefault("STEMMY_DB_STATUS", "1")
        os.environ.setdefault("STEMMY_DB_PREFER_SQLITE", "1")
        
        # State for multi-step workflows
        self.selected_format: Optional[Dict] = None
        self.selected_fragments: List[Dict] = []
        self.last_output: Optional[str] = None
        self.force_sentence_mode: bool = False
        
        # Available music files
        self.music_files = self._scan_music()

        # Log file for errors
        self._log_path = self._get_log_path()
    
    def _debug(self, msg: str):
        """Print debug message if debug mode is on."""
        if self.debug:
            console.print(f"[dim cyan]DEBUG: {msg}[/dim cyan]")

    def _get_log_path(self) -> Path:
        """Get log file path for RPG errors."""
        from stemmy_cli.paths import get_project_root
        log_dir = get_project_root() / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "stemmy_rpg.log"

    def _log_exception(self, context: str, exc: BaseException) -> Path:
        """Append exception details to RPG log file."""
        ts = datetime.utcnow().isoformat()
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] {context}\n")
            f.write(f"{repr(exc)}\n")
            f.write(traceback.format_exc())
            f.write("\n")
        return self._log_path

    def _tool_default(self, name: str) -> Any:
        """Default return on tool failure."""
        if name in ("get_database_stats",):
            return {}
        if name.startswith(("list_", "search_", "analyze_")):
            return []
        return {"success": False, "error": "Tool execution failed"}

    def _execute_tool_safe(self, name: str, **kwargs) -> Any:
        """Execute tool with error handling and logging."""
        try:
            console.print(f"[dim]Database: {name}...[/dim]")
            from stemmy_cli.agent.tools import execute_tool
            return execute_tool(name, **kwargs)
        except Exception as e:
            log_path = self._log_exception(f"Tool '{name}' failed", e)
            console.print(f"[red]Database error: {e}[/red]")
            console.print(f"[dim]Log: {log_path}[/dim]")
            return self._tool_default(name)

    def _dedupe_results(self, results: List[Dict[str, Any]], tolerance_ms: int = 50) -> List[Dict[str, Any]]:
        """Deduplicate results by audio_url + timing."""
        seen = []
        unique = []
        for r in results:
            audio_url = r.get("audio_url") or r.get("item_audio_url") or r.get("source_audio_url") or ""
            try:
                start_ms = int(float(r.get("start_time", 0) or 0) * 1000)
                end_ms = int(float(r.get("end_time", 0) or 0) * 1000)
            except (TypeError, ValueError):
                start_ms, end_ms = 0, 0
            is_dup = False
            for seen_url, seen_start, seen_end in seen:
                if seen_url == audio_url:
                    if abs(seen_start - start_ms) <= tolerance_ms and abs(seen_end - end_ms) <= tolerance_ms:
                        is_dup = True
                        break
            if not is_dup:
                seen.append((audio_url, start_ms, end_ms))
                unique.append(r)
        return unique

    def _prompt_limit(self, default_limit: int, max_limit: int) -> int:
        console.print(f"[cyan]Hoeveel resultaten?[/cyan] [dim](default: {default_limit}, max: {max_limit})[/dim]")
        limit_str = self._prompt("Limit")
        if limit_str.isdigit():
            return min(int(limit_str), max_limit)
        return default_limit

    def _prompt_duration_filters(
        self,
        default_min: Optional[float] = None,
        default_max: Optional[float] = None,
    ) -> tuple:
        console.print()
        min_label = f"{default_min}s" if default_min is not None else "geen"
        max_label = f"{default_max}s" if default_max is not None else "geen"
        console.print(f"[cyan]Filter op duur?[/cyan] [dim](min: {min_label}, max: {max_label})[/dim]")
        min_str = self._prompt("Min sec")
        max_str = self._prompt("Max sec")
        min_sec = float(min_str) if min_str.replace(".", "", 1).isdigit() else default_min
        max_sec = float(max_str) if max_str.replace(".", "", 1).isdigit() else default_max
        return min_sec, max_sec

    def _prompt_sentence_mode(self) -> bool:
        console.print()
        console.print("[cyan]Zinnen forceren?[/cyan] [dim](default: nee)[/dim]")
        return self._prompt("Zinnen") in ("j", "ja", "y", "yes")

    def _filter_by_duration(
        self,
        results: List[Dict[str, Any]],
        min_sec: Optional[float],
        max_sec: Optional[float],
    ) -> List[Dict[str, Any]]:
        if min_sec is None and max_sec is None:
            return results
        filtered = []
        for r in results:
            try:
                start = float(r.get("start_time", 0) or 0)
                end = float(r.get("end_time", 0) or 0)
            except (TypeError, ValueError):
                continue
            dur = end - start
            if min_sec is not None and dur < min_sec:
                continue
            if max_sec is not None and dur > max_sec:
                continue
            filtered.append(r)
        return filtered

    def _extract_sentences_from_results(
        self,
        results: List[Dict[str, Any]],
        min_sec: Optional[float],
        max_sec: Optional[float],
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Explode fragment results into sentence-level segments using words."""
        if not force and min_sec is None and max_sec is None:
            return results
        try:
            from stemmy_cli.agent.tools import _split_sentences
        except Exception:
            return results
        extracted = []
        max_sent = max_sec if max_sec is not None else 60.0
        min_sent = min_sec if min_sec is not None else 0.0
        for r in results:
            words_json = r.get("words")
            if not words_json:
                continue
            try:
                words = json.loads(words_json) if isinstance(words_json, str) else words_json
            except (json.JSONDecodeError, TypeError):
                continue
            audio_url = r.get("item_audio_url") or r.get("audio_url") or r.get("source_audio_url")
            for sent in _split_sentences(words, max_sentence_seconds=max_sent):
                dur = sent.get("duration", 0)
                if dur < min_sent:
                    continue
                if max_sec is not None and dur > max_sec:
                    continue
                extracted.append({
                    "text": sent["text"],
                    "start_time": sent["start_time"],
                    "end_time": sent["end_time"],
                    "item_audio_url": audio_url,
                    "audio_url": audio_url,
                    "fragment_id": r.get("id") or r.get("fragment_id"),
                    "item_id": r.get("item_id"),
                })
        return extracted

    def _apply_sentence_mode(
        self,
        results: List[Dict[str, Any]],
        min_sec: Optional[float],
        max_sec: Optional[float],
        force: bool,
    ) -> List[Dict[str, Any]]:
        if not results:
            return results
        if not force and min_sec is None and max_sec is None:
            return results
        sentence_results = self._extract_sentences_from_results(
            results,
            min_sec,
            max_sec,
            force=force,
        )
        if sentence_results:
            sentence_results = self._dedupe_results(sentence_results)
            console.print(f"[dim]Zinnen gevonden: {len(sentence_results)}[/dim]")
            return sentence_results
        return self._filter_by_duration(results, min_sec, max_sec)

    def _result_label(
        self,
        min_sec: Optional[float],
        max_sec: Optional[float],
        force_sent: bool,
        default: str = "fragmenten",
    ) -> str:
        if force_sent or min_sec is not None or max_sec is not None:
            return "resultaten"
        return default

    def _gather_word_segments(
        self,
        queries: List[str],
        format_id: Optional[str],
        limit_per_query: int,
    ) -> List[Dict[str, Any]]:
        """Search fragments for multiple queries and extract word timings."""
        try:
            from stemmy_cli.agent.tools import _extract_word_timings
        except Exception:
            _extract_word_timings = None
        all_segments: List[Dict[str, Any]] = []
        for q in queries:
            segs = self._execute_tool_safe("search_fragments", query=q, format_id=format_id, limit=limit_per_query)
            if not segs:
                continue
            if _extract_word_timings:
                word_segs = _extract_word_timings(segs, q)
                if word_segs:
                    segs = word_segs
            for seg in segs:
                seg["source_query"] = q
            all_segments.extend(segs)
        return all_segments

    def _sentence_matches_queries(self, text: str, queries: List[str]) -> bool:
        """Check if sentence text matches any query (loose token match)."""
        if not text:
            return False
        # Normalize text to alnum + spaces
        normalized = "".join(c.lower() if c.isalnum() else " " for c in text)
        normalized_no_space = normalized.replace(" ", "")
        words = set(w for w in normalized.split() if w)
        for q in queries:
            q_norm = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in q).strip()
            if not q_norm:
                continue
            q_tokens = [t for t in q_norm.split() if t]
            if not q_tokens:
                continue
            if len(q_tokens) == 1:
                if q_tokens[0] in words or q_tokens[0] in normalized or q_tokens[0] in normalized_no_space:
                    return True
            else:
                if all(t in words for t in q_tokens):
                    return True
        return False

    def _show_results_paged(self, results: List[Dict[str, Any]], mode: str):
        page_size = 20
        idx = 0
        total = len(results)
        while idx < total:
            slice_end = min(idx + page_size, total)
            table = Table(show_header=True, header_style="bold", box=None)
            table.add_column("#", style="cyan", width=4)
            if mode in ("questions", "exclamations"):
                table.add_column("Zin", width=80)
            else:
                table.add_column("Tekst", width=80)
            for i, r in enumerate(results[idx:slice_end], idx + 1):
                text = r.get("text", r.get("sentence", ""))[:78]
                if len(r.get("text", r.get("sentence", ""))) > 78:
                    text += "..."
                table.add_row(str(i), text)
            console.print(table)
            idx = slice_end
            if idx >= total:
                break
            console.print("[dim]Enter = volgende pagina | q = stoppen | a = alles[/dim]")
            choice = self._prompt("Meer").strip().lower()
            if choice == "q":
                break
            if choice == "a":
                page_size = total

    def _select_music_for_compilation(self) -> Optional[Dict[str, Any]]:
        console.print()
        console.print("[cyan]Achtergrondmuziek?[/cyan]")
        console.print("  [cyan]0[/cyan]  Geen muziek")
        from stemmy_cli.paths import get_music_dir
        music_dir = get_music_dir()
        music_files = list(music_dir.glob("*.mp3")) if music_dir.exists() else []
        for i, m in enumerate(music_files[:10], 1):
            console.print(f"  [cyan]{i}[/cyan]  {m.name}")
        choice = self._prompt("Nummer")
        if choice == "0" or not choice:
            return None
        if not choice.isdigit() or not (1 <= int(choice) <= len(music_files)):
            console.print("[yellow]Ongeldige keuze[/yellow]")
            return None
        music_path = str(music_files[int(choice) - 1])
        console.print("[cyan]Muziek volume?[/cyan] [dim](default: 15%)[/dim]")
        vol_str = self._prompt("Volume %")
        volume = int(vol_str) if vol_str.isdigit() else 15
        return {"music_path": music_path, "volume": volume / 100.0}

    def _prompt_fx_preset(self) -> Optional[str]:
        console.print()
        console.print("[cyan]Special FX?[/cyan]")
        console.print("  [cyan]0[/cyan]  Geen")
        console.print("  [cyan]1[/cyan]  Radio (telefoon)")
        console.print("  [cyan]2[/cyan]  Warm")
        console.print("  [cyan]3[/cyan]  Bright")
        choice = self._prompt("FX")
        return choice if choice in ("1", "2", "3") else None

    def _apply_fx_preset(self, input_path: str, choice: Optional[str]) -> str:
        if not choice:
            return input_path
        console.print()
        output_path = input_path.replace(".mp3", "_fx_tmp.mp3")
        if choice == "1":
            filter_str = "highpass=f=300,lowpass=f=3400,acompressor=threshold=-18dB:ratio=3:attack=5:release=50"
        elif choice == "2":
            filter_str = "acompressor=threshold=-20dB:ratio=2.5:attack=5:release=50,equalizer=f=120:width_type=o:width=2:g=2"
        else:
            filter_str = "equalizer=f=8000:width_type=o:width=2:g=3,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=50"
        import subprocess
        cmd = ["ffmpeg", "-y", "-i", input_path, "-af", filter_str, "-c:a", "libmp3lame", "-q:a", "2", output_path]
        subprocess.run(cmd, capture_output=True)
        if Path(output_path).exists():
            Path(output_path).replace(input_path)
            return input_path
        return input_path

    def _interleave_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Shuffle segments while avoiding consecutive items from the same source."""
        import random
        if not segments:
            return segments
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for seg in segments:
            key = (
                seg.get("item_id")
                or seg.get("audio_url")
                or seg.get("item_audio_url")
                or seg.get("source_audio_url")
                or "unknown"
            )
            buckets.setdefault(str(key), []).append(seg)
        # Shuffle within each bucket
        for k in buckets:
            random.shuffle(buckets[k])
        keys = list(buckets.keys())
        random.shuffle(keys)
        output: List[Dict[str, Any]] = []
        while keys:
            next_keys = []
            for k in keys:
                bucket = buckets.get(k)
                if not bucket:
                    continue
                output.append(bucket.pop())
                if bucket:
                    next_keys.append(k)
            keys = next_keys
            random.shuffle(keys)
        return output

    def _concat_audio_files(self, file_list: Path, output_path: str) -> None:
        """Concatenate audio files with re-encode to avoid timestamp gaps."""
        import subprocess
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(file_list),
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    
    def _scan_music(self) -> List[Path]:
        """Find available background music in static/music/."""
        from stemmy_cli.paths import get_music_dir
        music_dir = get_music_dir()
        if music_dir.exists():
            return list(music_dir.glob("*.mp3"))
        return []

    def _get_output_dir(self) -> Path:
        """Get output directory for generated files."""
        from stemmy_cli.paths import get_output_dir
        return get_output_dir()
    
    def _create_compilation_with_progress(
        self,
        queries: List[str],
        compilation_type: str = "word",
        use_word_timing: bool = True,
        format_id: Optional[str] = None,
        limit_per_query: int = 10,
        output_path: Optional[str] = None,
        min_sec: Optional[float] = None,
        max_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Create compilation with step-by-step progress display."""
        from stemmy_cli.agent.tools import (
            _search_fragments, _search_entities, _extract_word_timings
        )
        from stemmy_cli.audio.local_extract import extract_segments_local
        import subprocess
        import tempfile
        
        console.print()
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(bar_width=25, complete_style="green"),
            TextColumn("[dim]{task.fields[detail]}[/dim]"),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task("Starten...", total=100, detail="")
            
            # Step 1: Search (30%)
            progress.update(task, description="🔍 Zoeken", detail=f"{len(queries)} queries")
            all_segments = []
            
            for query in queries:
                try:
                    if compilation_type == "entity":
                        segs = _search_entities(query, format_id=format_id, limit=limit_per_query)
                    else:
                        segs = _search_fragments(query, format_id=format_id, limit=limit_per_query)
                    
                    # Extract word timings if requested
                    if use_word_timing and segs:
                        word_segs = _extract_word_timings(segs, query)
                        if word_segs:
                            segs = word_segs
                except Exception as e:
                    log_path = self._log_exception("Search query failed", e)
                    progress.update(task, description="[red]✗ Database fout[/red]", completed=100, detail="")
                    console.print(f"[red]Database error: {e}[/red]")
                    console.print(f"[dim]Log: {log_path}[/dim]")
                    return {"success": False, "error": "Database query failed"}
                
                for seg in segs:
                    seg["source_query"] = query
                all_segments.extend(segs)
            
            all_segments = self._filter_by_duration(all_segments, min_sec, max_sec)
            progress.update(task, completed=30, detail=f"{len(all_segments)} gevonden")
            
            if not all_segments:
                progress.update(task, description="[red]✗ Geen resultaten[/red]", completed=100, detail="")
                return {"success": False, "error": "No matches found"}
            
            # Shuffle without repeating same source too much
            all_segments = self._interleave_segments(all_segments)
            
            # Step 2: Extract audio (50%)
            progress.update(task, description="🎵 Audio extracten (parallel)", completed=35, detail=f"0/{len(all_segments)}")
            
            temp_dir = Path(tempfile.gettempdir()) / f"stemmy_rpg_{id(self)}"
            temp_dir.mkdir(exist_ok=True)
            
            segments_to_extract = []
            for i, seg in enumerate(all_segments):
                audio_url = seg.get("audio_url") or seg.get("item_audio_url") or seg.get("source_audio_url")
                start = float(seg.get("start_time", 0) or 0)
                end = float(seg.get("end_time", 0) or 0)
                if start > 10000:
                    start, end = start / 1000, end / 1000
                if not audio_url or end <= start:
                    continue
                segments_to_extract.append({
                    "id": str(i),
                    "audio_url": audio_url,
                    "start_time": start,
                    "end_time": end,
                })
            
            def update_progress(current: int, total: int, _msg: str) -> None:
                pct = 35 + (50 * current / max(total, 1))
                progress.update(task, completed=pct, detail=f"{current}/{total}")
            
            successes, failures = extract_segments_local(
                segments_to_extract,
                output_dir=temp_dir,
                progress_callback=update_progress,
            )
            
            extract_results = [Path(r["output_path"]) for r in successes]
            extracted = len(extract_results)
            
            if not extract_results:
                progress.update(task, description="[red]✗ Extractie mislukt[/red]", completed=100, detail="")
                return {"success": False, "error": "Failed to extract audio"}
            
            # Step 3: Concatenate (20%)
            progress.update(task, description="🔗 Samenvoegen", completed=85, detail=f"{extracted} clips")
            
            # Create concat file
            file_list = temp_dir / "files.txt"
            with open(file_list, "w") as f:
                for seg_path in extract_results:
                    f.write(f"file '{seg_path}'\n")
            
            # Output path
            if not output_path:
                output_dir = _get_output_dir()
                query_tag = "_".join("".join(c if c.isalnum() else "_" for c in q[:10]) for q in queries[:3])
                output_path = str(output_dir / f"shuffled_{query_tag}.mp3")
            
            try:
                self._concat_audio_files(file_list, output_path)
            except Exception as e:
                progress.update(task, description="[red]✗ Samenvoegen mislukt[/red]", completed=100, detail="")
                return {"success": False, "error": str(e)}
            
            progress.update(task, description="[green]✓ Klaar![/green]", completed=100, detail="")
        
        # Cleanup temp files
        try:
            for f in temp_dir.glob("*.mp3"):
                f.unlink()
            file_list.unlink()
            temp_dir.rmdir()
        except Exception:
            pass
        
        return {
            "success": True,
            "output_path": output_path,
            "segments_used": extracted,
            "total_found": len(all_segments),
        }
    
    def run(self):
        """Main loop."""
        self._show_welcome()
        
        while self.running:
            try:
                self._show_main_menu()
                choice = self._prompt("Keuze")
                self._handle_main_choice(choice)
            except KeyboardInterrupt:
                console.print("\n[dim]Ctrl+C → type 'q' om te stoppen[/dim]")
            except EOFError:
                self.running = False
        
        console.print("\n[dim]Tot ziens![/dim]\n")
    
    def _prompt(self, label: str = "") -> str:
        """Get user input with RPG-style prompt."""
        try:
            text = self.session.prompt(
                HTML(f"<prompt>⚔ {label}> </prompt>") if label else HTML("<prompt>⚔ </prompt>")
            )
            return text.strip().lower()
        except (KeyboardInterrupt, EOFError):
            return ""
    
    def _show_welcome(self):
        """RPG-style welcome."""
        console.print()
        console.print(Panel.fit(
            "[bold cyan]STEMMY RPG MODE[/bold cyan]\n\n"
            "[dim]Menu-driven • Voorspelbaar • Geen AI-kosten[/dim]",
            border_style="cyan"
        ))
        console.print()
    
    def _show_main_menu(self):
        """Show main menu options."""
        console.print()
        console.print("[bold]═══ HOOFDMENU ═══[/bold]")
        console.print()
        console.print("  [cyan]1[/cyan]  🎙️  Compilatie maken")
        console.print("  [cyan]2[/cyan]  🔍  Zoeken in fragmenten")
        console.print("  [cyan]3[/cyan]  🎯  Slimme zoekopdrachten")
        console.print("  [cyan]4[/cyan]  📋  Formats bekijken")
        console.print("  [cyan]5[/cyan]  🎵  Muziek toevoegen aan bestand")
        console.print("  [cyan]6[/cyan]  📊  Database stats")
        console.print()
        console.print("  [dim]q = quit[/dim]")
        console.print()
    
    def _handle_main_choice(self, choice: str):
        """Route main menu choice."""
        if choice in ("q", "quit", "exit"):
            self.running = False
        elif choice == "1":
            self._compilation_wizard()
        elif choice == "2":
            self._search_wizard()
        elif choice == "3":
            self._smart_search_wizard()
        elif choice == "4":
            self._formats_browser()
        elif choice == "5":
            self._music_wizard()
        elif choice == "6":
            self._show_stats()
        elif choice == "":
            pass
        else:
            console.print("[yellow]Kies 1-6 of 'q'[/yellow]")
    
    # ═══════════════════════════════════════════════════════════════
    # WIZARD 1: COMPILATIE MAKEN
    # ═══════════════════════════════════════════════════════════════
    
    def _compilation_wizard(self):
        """Step-by-step compilation wizard."""
        console.print()
        console.print("[bold]═══ COMPILATIE WIZARD ═══[/bold]")
        console.print()
        
        # Step 1: Choose type
        console.print("[cyan]Stap 1:[/cyan] Wat wil je maken?")
        console.print()
        console.print("  [cyan]1[/cyan]  Woorden extractie (bijv. 'hoe dan ook', 'Google')")
        console.print("  [cyan]2[/cyan]  Fragmenten compilatie (hele zinnen)")
        console.print("  [cyan]3[/cyan]  Entity shuffle (namen, organisaties, etc)")
        console.print("  [cyan]4[/cyan]  🧠 Semantisch zoeken (op betekenis)")
        console.print("  [cyan]5[/cyan]  🔀 Hybrid (semantisch + zoekwoord)")
        console.print("  [cyan]0[/cyan]  ← Terug")
        console.print()
        
        comp_type = self._prompt("Type")
        if comp_type == "0":
            return
        
        if comp_type not in ("1", "2", "3", "4", "5"):
            console.print("[yellow]Kies 1-5 of 0[/yellow]")
            return
        
        if comp_type == "1":
            self._word_compilation()
        elif comp_type == "2":
            self._fragment_compilation()
        elif comp_type == "3":
            self._entity_compilation()
        elif comp_type == "4":
            self._semantic_compilation()
        elif comp_type == "5":
            self._hybrid_compilation()
    
    def _word_compilation(self):
        """Word-level extraction wizard."""
        console.print()
        console.print("[cyan]Stap 2:[/cyan] Welke woorden/phrases?")
        console.print("[dim]Meerdere scheiden met komma, bijv: hoe dan ook, anyway, eigenlijk[/dim]")
        console.print()
        
        queries_input = self._prompt("Woorden")
        if not queries_input:
            return
        
        queries = [q.strip() for q in queries_input.split(",") if q.strip()]
        
        # Step 3: Optional format filter
        format_id = self._select_format_optional()
        
        # Step 4: Limit
        console.print()
        console.print("[cyan]Stap 3:[/cyan] Hoeveel per woord?[/cyan]")
        limit = self._prompt_limit(default_limit=100, max_limit=1000)

        min_sec, max_sec = self._prompt_duration_filters(default_min=None, default_max=None)
        force_sent = self._prompt_sentence_mode()

        console.print()
        console.print("[dim]Zoeken...[/dim]")
        if force_sent or min_sec is not None or max_sec is not None:
            # Sentence-level matches containing the query terms
            all_frags: List[Dict[str, Any]] = []
            for q in queries:
                segs = self._execute_tool_safe("search_fragments", query=q, format_id=format_id, limit=limit)
                for seg in segs:
                    seg["source_query"] = q
                all_frags.extend(segs)
            if not all_frags:
                console.print("[yellow]Geen resultaten gevonden[/yellow]")
                return
            all_frags = self._dedupe_results(all_frags)
            results = self._apply_sentence_mode(all_frags, min_sec, max_sec, force=True)
            results = [r for r in results if self._sentence_matches_queries(r.get("text", ""), queries)]
        else:
            results = self._gather_word_segments(queries, format_id, limit)

        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            return

        results = self._dedupe_results(results)

        console.print()
        label = self._result_label(min_sec, max_sec, force_sent, default="matches")
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()

        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Woord", width=20)
        table.add_column("Context", width=40)
        for i, r in enumerate(results[:15], 1):
            word = r.get("text", r.get("word", ""))[:18]
            context = r.get("context", r.get("fragment_text", r.get("text", "")))[:38]
            if len(r.get("context", r.get("fragment_text", r.get("text", "")))) > 38:
                context += "..."
            table.add_row(str(i), word, context)
        console.print(table)

        if len(results) > 15:
            console.print(f"[dim]+{len(results) - 15} meer...[/dim]")

        self.selected_fragments = results
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="word")

        console.print()
        console.print("Wil je hiervan een compilatie maken? [cyan]j[/cyan]/n")
        if self._prompt("Compilatie") in ("j", "ja", "y", "yes"):
            self._make_compilation_from_results(results, mode="word", pattern=",".join(queries))
    
    def _fragment_compilation(self):
        """Fragment-level compilation."""
        console.print()
        console.print("[cyan]Stap 2:[/cyan] Zoekterm voor fragmenten?")
        console.print()
        
        query = self._prompt("Zoekterm")
        if not query:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        console.print("[cyan]Stap 3:[/cyan] Hoeveel fragmenten? [dim](default: 100, max: 1000)[/dim]")
        limit = self._prompt_limit(default_limit=100, max_limit=1000)
        
        min_sec, max_sec = self._prompt_duration_filters()
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Zoeken...[/dim]")
        
        results = self._execute_tool_safe("search_fragments", query=query, format_id=format_id, limit=limit)
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            return
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent)
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="fragment")
        
        self._make_compilation_from_results(results, mode="fragment", pattern=query)
    
    def _entity_compilation(self):
        """Entity-based compilation."""
        console.print()
        console.print("[cyan]Stap 2:[/cyan] Welk type entity?")
        console.print()
        console.print("  [cyan]1[/cyan]  person_name (namen)")
        console.print("  [cyan]2[/cyan]  organization (bedrijven)")
        console.print("  [cyan]3[/cyan]  location (plaatsen)")
        console.print("  [cyan]4[/cyan]  product (producten)")
        console.print("  [cyan]5[/cyan]  alle types")
        console.print()
        
        type_map = {
            "1": "person_name",
            "2": "organization",
            "3": "location",
            "4": "product",
            "5": None
        }
        
        type_choice = self._prompt("Type")
        entity_type = type_map.get(type_choice)
        if type_choice not in type_map:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        console.print("[cyan]Stap 3:[/cyan] Hoeveel entities? [dim](default: 20)[/dim]")
        limit_str = self._prompt("Limit")
        limit = int(limit_str) if limit_str.isdigit() else 20
        
        min_sec, max_sec = self._prompt_duration_filters(default_min=0.3, default_max=3.0)
        force_sent = True if mode in ("questions", "exclamations") else self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Zoeken...[/dim]")
        
        results = self._execute_tool_safe("search_entities", query="", entity_type=entity_type, format_id=format_id, limit=limit)
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            return
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent, default="entities")
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="entities")
        
        self._make_compilation_from_results(results, mode="entities", pattern=entity_type or "all")
    
    def _semantic_compilation(self):
        """Semantic search compilation - find by meaning, not exact words."""
        console.print()
        console.print("[cyan]Stap 2:[/cyan] Welk onderwerp of concept?")
        console.print("[dim]Bijv: 'passionate debates about technology', 'skeptical reactions', 'discussions about AI'[/dim]")
        console.print()
        
        query = self._prompt("Concept")
        if not query:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        console.print("[cyan]Stap 3:[/cyan] Hoeveel fragmenten? [dim](default: 100, max: 1000)[/dim]")
        limit = self._prompt_limit(default_limit=100, max_limit=1000)
        
        min_sec, max_sec = self._prompt_duration_filters()
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Semantisch zoeken...[/dim]")
        
        results = self._execute_tool_safe("search_semantic", query=query, format_id=format_id, limit=limit)
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            console.print("[dim]Tip: Run 'stemmy embeddings build' als embeddings nog niet bestaan[/dim]")
            return
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent)
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="semantic")
        
        self._make_compilation_from_results(results, mode="semantic", pattern=query)
    
    def _hybrid_compilation(self):
        """Hybrid: semantic + keyword search combined."""
        console.print()
        console.print("[cyan]Stap 2:[/cyan] Zoekterm of concept?")
        console.print("[dim]Combineert betekenis + exacte woorden (bijv: 'technologie', 'AI discussie')[/dim]")
        console.print()
        
        query = self._prompt("Zoek")
        if not query:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        console.print("[cyan]Stap 3:[/cyan] Hoeveel fragmenten? [dim](default: 100, max: 1000)[/dim]")
        limit = self._prompt_limit(default_limit=100, max_limit=1000)
        
        min_sec, max_sec = self._prompt_duration_filters()
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Hybrid zoeken...[/dim]")
        
        results = self._execute_tool_safe("search_hybrid", query=query, format_id=format_id, limit=limit)
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            return
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent)
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="hybrid")
        
        self._make_compilation_from_results(results, mode="hybrid", pattern=query)
    
    def _select_format_optional(self) -> Optional[str]:
        """Let user optionally select a format."""
        console.print()
        console.print("Wil je filteren op specifieke podcast? [cyan]j[/cyan]/n")
        
        if self._prompt("Filter") not in ("j", "ja", "y", "yes"):
            return None
        formats = self._execute_tool_safe("list_formats", limit=20)
        if not formats:
            console.print("[yellow]Geen podcasts beschikbaar (database leeg of fout)[/yellow]")
            return None
        
        console.print()
        console.print("[bold]Beschikbare podcasts:[/bold]")
        console.print()
        
        for i, f in enumerate(formats[:15], 1):
            title = f.get("title", "")[:40]
            console.print(f"  [cyan]{i:2}[/cyan]  {title}")
        
        console.print()
        choice = self._prompt("Nummer")
        
        if choice.isdigit() and 1 <= int(choice) <= len(formats):
            selected = formats[int(choice) - 1]
            console.print(f"[dim]Geselecteerd: {selected.get('title')}[/dim]")
            return selected.get("id")
        
        return None
    
    # ═══════════════════════════════════════════════════════════════
    # WIZARD 2: ZOEKEN
    # ═══════════════════════════════════════════════════════════════
    
    def _search_wizard(self):
        """Search fragments."""
        console.print()
        console.print("[bold]═══ ZOEKEN ═══[/bold]")
        console.print()
        console.print("[cyan]Zoekmodus:[/cyan]")
        console.print("  [cyan]1[/cyan]  Tekst (exacte woorden)")
        console.print("  [cyan]2[/cyan]  Semantisch (op betekenis)")
        console.print()
        mode = self._prompt("Modus") or "1"
        console.print()
        console.print("[cyan]Zoekterm of concept:[/cyan]")
        
        query = self._prompt("Zoek")
        if not query:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        limit = self._prompt_limit(default_limit=100, max_limit=1000)
        min_sec, max_sec = self._prompt_duration_filters()
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Zoeken...[/dim]")
        
        if mode == "2":
            results = self._execute_tool_safe("search_semantic", query=query, format_id=format_id, limit=limit)
        else:
            results = self._execute_tool_safe("search_fragments", query=query, format_id=format_id, limit=limit)
        
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            return
        
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent)
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Tekst", width=60)
        table.add_column("Duur", style="dim", width=8)
        
        for i, r in enumerate(results[:10], 1):
            text = r.get("text", "")[:55]
            if len(r.get("text", "")) > 55:
                text += "..."
            
            start = float(r.get("start_time", 0) or 0)
            end = float(r.get("end_time", 0) or 0)
            duration = f"{end - start:.1f}s" if end > start else "-"
            
            table.add_row(str(i), text, duration)
        
        console.print(table)
        
        if len(results) > 10:
            console.print(f"[dim]+{len(results) - 10} meer...[/dim]")
        
        self.selected_fragments = results
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="search")
        
        # Offer to make compilation from results
        if results:
            console.print()
            console.print("Wil je hiervan een compilatie maken? [cyan]j[/cyan]/n")
            if self._prompt("Compilatie") in ("j", "ja", "y", "yes"):
                self._make_compilation_from_results(results)
    
    # ═══════════════════════════════════════════════════════════════
    # WIZARD 3: SLIMME ZOEKOPDRACHTEN
    # ═══════════════════════════════════════════════════════════════
    
    def _smart_search_wizard(self):
        """Advanced pattern-based search wizard."""
        console.print()
        console.print("[bold]═══ SLIMME ZOEKOPDRACHTEN ═══[/bold]")
        console.print()
        console.print("[bold]Woord-patronen:[/bold]")
        console.print("  [cyan]1[/cyan]  🔤  Woord begint met... (bijv: 'on' → ongelofelijk, onzin)")
        console.print("  [cyan]2[/cyan]  🔤  Woord eindigt op... (bijv: 'lijk' → eigenlijk, werkelijk)")
        console.print("  [cyan]3[/cyan]  🔤  Woord bevat... (bijv: 'tech' → technologie, biotech)")
        console.print("  [cyan]4[/cyan]  🎭  Alliteratie (woorden met zelfde beginletter)")
        console.print("  [cyan]5[/cyan]  📏  Lange woorden (10+ letters)")
        console.print("  [cyan]6[/cyan]  📏  Korte woorden (1-3 letters)")
        console.print()
        console.print("[bold]Zin-patronen:[/bold]")
        console.print("  [cyan]7[/cyan]  ❓  Vragen (zinnen met ?)")
        console.print("  [cyan]8[/cyan]  ❗  Uitroepen (zinnen met !)")
        console.print()
        console.print("[bold]Semantisch:[/bold]")
        console.print("  [cyan]9[/cyan]  🧠  Zoek op betekenis (bijv: 'passionate debates', 'skeptical reactions')")
        console.print()
        console.print("  [cyan]0[/cyan]  ← Terug")
        console.print()
        
        choice = self._prompt("Keuze")
        
        if choice == "0":
            return
        
        # Map choices to modes - "semantic" is special
        if choice == "9":
            self._semantic_smart_search()
            return
        
        mode_map = {
            "1": ("starts_with", True, "Voorvoegsel"),
            "2": ("ends_with", True, "Achtervoegsel"),
            "3": ("contains", True, "Bevat"),
            "4": ("alliteration", False, None),
            "5": ("long_words", False, None),
            "6": ("short_words", False, None),
            "7": ("questions", False, None),
            "8": ("exclamations", False, None),
        }
        
        if choice not in mode_map:
            console.print("[yellow]Ongeldige keuze[/yellow]")
            return
        
        mode, needs_pattern, pattern_prompt = mode_map[choice]
        pattern = ""
        
        if needs_pattern:
            console.print()
            console.print(f"[cyan]{pattern_prompt}:[/cyan]")
            pattern = self._prompt("Patroon")
            if not pattern:
                return
        
        # Optional format filter
        format_id = self._select_format_optional()

        # Result limit
        console.print()
        default_limit = 100 if mode not in ("questions", "exclamations") else 300
        limit = self._prompt_limit(default_limit=default_limit, max_limit=1000)
        min_sec, max_sec = self._prompt_duration_filters(default_min=0.3, default_max=3.0)
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Zoeken...[/dim]")
        
        # Debug output
        self._debug(f"mode={mode}, pattern='{pattern}', format_id={format_id}")
        
        # Use lexical analyzer for word patterns, text analyzer for sentence patterns
        if mode in ("questions", "exclamations"):
            self._debug(f"Calling analyze_text(mode={mode}, format_id={format_id}, limit={limit})")
            results = self._execute_tool_safe("analyze_text", mode=mode, format_id=format_id, limit=limit)
        else:
            self._debug(f"Calling analyze_lexical(mode={mode}, pattern={pattern}, format_id={format_id}, limit={limit})")
            results = self._execute_tool_safe("analyze_lexical", mode=mode, pattern=pattern, format_id=format_id, limit=limit)
        
        self._debug(f"Received {len(results) if results else 0} results")
        
        if not results:
            console.print("[yellow]Geen resultaten gevonden[/yellow]")
            if self.debug:
                console.print("[dim]Tip: Probeer zonder podcast filter of met ander patroon[/dim]")
            return
        
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent, default="matches")
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        
        # Display results based on type
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", style="cyan", width=3)
        
        if mode in ("questions", "exclamations"):
            table.add_column("Zin", width=65)
            for i, r in enumerate(results[:15], 1):
                text = r.get("text", r.get("sentence", ""))[:62]
                if len(r.get("text", r.get("sentence", ""))) > 62:
                    text += "..."
                table.add_row(str(i), text)
        else:
            table.add_column("Woord", width=25)
            table.add_column("Context", width=40)
            for i, r in enumerate(results[:15], 1):
                word = r.get("word", r.get("text", ""))[:23]
                context = r.get("context", r.get("fragment_text", ""))[:38]
                if len(r.get("context", r.get("fragment_text", ""))) > 38:
                    context += "..."
                table.add_row(str(i), word, context)
        
        console.print(table)
        
        if len(results) > 15:
            console.print(f"[dim]+{len(results) - 15} meer...[/dim]")
        
        self.selected_fragments = results
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode=mode)
        
        # Offer to make compilation
        console.print()
        console.print("Wil je hiervan een compilatie maken? [cyan]j[/cyan]/n")
        if self._prompt("Compilatie") in ("j", "ja", "y", "yes"):
            self._make_compilation_from_results(results, mode=mode, pattern=pattern)
    
    def _semantic_smart_search(self):
        """Semantic search within smart search wizard."""
        console.print()
        console.print("[cyan]Concept of onderwerp:[/cyan]")
        console.print("[dim]Bijv: 'passionate debates about technology', 'skeptical reactions to AI'[/dim]")
        console.print()
        
        query = self._prompt("Concept")
        if not query:
            return
        
        format_id = self._select_format_optional()
        
        console.print()
        limit = self._prompt_limit(default_limit=100, max_limit=1000)
        min_sec, max_sec = self._prompt_duration_filters()
        force_sent = self._prompt_sentence_mode()
        
        console.print()
        console.print("[dim]Semantisch zoeken...[/dim]")
        results = self._execute_tool_safe("search_semantic", query=query, format_id=format_id, limit=limit)
        
        if not results:
            console.print("[yellow]Geen resultaten gevonden (semantisch).[/yellow]")
            console.print("[dim]Fallback: probeer hybride zoeken...[/dim]")
            results = self._execute_tool_safe("search_hybrid", query=query, format_id=format_id, limit=limit)
            if not results:
                console.print("[yellow]Ook hybride gaf niets op.[/yellow]")
                console.print("[dim]Tip: Run 'stemmy embeddings build' als embeddings nog niet bestaan[/dim]")
                return
        
        results = self._dedupe_results(results)
        results = self._apply_sentence_mode(results, min_sec, max_sec, force_sent)
        
        console.print()
        label = self._result_label(min_sec, max_sec, force_sent)
        console.print(f"[green]Gevonden: {len(results)} {label}[/green]")
        console.print()
        
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Score", style="green", width=6)
        table.add_column("Tekst", width=60)
        
        for i, r in enumerate(results[:15], 1):
            text = r.get("text", "")[:58]
            if len(r.get("text", "")) > 58:
                text += "..."
            score = r.get("similarity", 0)
            table.add_row(str(i), f"{score:.2f}", text)
        
        console.print(table)
        
        if len(results) > 15:
            console.print(f"[dim]+{len(results) - 15} meer...[/dim]")
        
        self.selected_fragments = results
        console.print()
        console.print("[cyan]Volledige lijst tonen?[/cyan] j/n")
        if self._prompt("Lijst") in ("j", "ja", "y", "yes"):
            self._show_results_paged(results, mode="semantic")
        
        console.print()
        console.print("Wil je hiervan een compilatie maken? [cyan]j[/cyan]/n")
        if self._prompt("Compilatie") in ("j", "ja", "y", "yes"):
            self._make_compilation_from_results(results, mode="semantic", pattern=query)
    
    def _make_compilation_from_results(
        self, 
        results: List[Dict], 
        mode: str = "", 
        pattern: str = ""
    ):
        """Create compilation from search/analyze results."""
        results = self._dedupe_results(results)
        console.print()
        console.print("[cyan]Hoeveel clips gebruiken?[/cyan] [dim](default: alle)[/dim]")
        limit_str = self._prompt("Limit")
        limit = int(limit_str) if limit_str.isdigit() else len(results)
        
        # Generate filename
        if mode and pattern:
            safe_pattern = "".join(c if c.isalnum() else "_" for c in pattern[:15])
            default_name = f"{mode}_{safe_pattern}.mp3"
        elif mode:
            default_name = f"{mode}_compilatie.mp3"
        else:
            default_name = "search_compilatie.mp3"
        
        console.print()
        console.print(f"[cyan]Bestandsnaam?[/cyan] [dim](default: {default_name})[/dim]")
        filename = self._prompt("Bestand")
        filename = filename if filename else default_name
        if not filename.endswith(".mp3"):
            filename += ".mp3"
        
        output_path = str(self._get_output_dir() / filename)

        # Pre-select music/FX and optional layering
        music_choice = self._select_music_for_compilation()
        fx_choice = self._prompt_fx_preset()
        console.print()
        console.print("Wil je een laag onder vanuit dezelfde batch? [cyan]j[/cyan]/n")
        use_layer = self._prompt("Laag") in ("j", "ja", "y", "yes")

        # Interleave to avoid same-source streaks
        segments_to_use = self._interleave_segments(results)[:limit]
        if use_layer and len(segments_to_use) < 2:
            use_layer = False

        actual_path = output_path
        segments_used = 0

        if use_layer:
            primary_segments = segments_to_use[::2]
            secondary_segments = segments_to_use[1::2]
            tmp_primary = output_path.replace(".mp3", "_layer1.mp3")
            tmp_secondary = output_path.replace(".mp3", "_layer2.mp3")

            res1 = self._create_compilation_from_segments(primary_segments, tmp_primary)
            if not res1.get("success"):
                console.print(f"[red]Error: {res1.get('error', 'Unknown')}[/red]")
                return
            res2 = self._create_compilation_from_segments(secondary_segments, tmp_secondary)
            if not res2.get("success"):
                console.print(f"[red]Error: {res2.get('error', 'Unknown')}[/red]")
                return

            mix_res = self._execute_tool_safe(
                "layer_audio_tracks",
                tracks=[tmp_primary, tmp_secondary],
                output_path=output_path,
                normalize=True,
            )
            if not mix_res.get("success"):
                console.print(f"[red]Duo-mix fout: {mix_res.get('error', 'Unknown')}[/red]")
                return
            segments_used = res1.get("segments_used", 0) + res2.get("segments_used", 0)

            # Cleanup temp layer files
            try:
                Path(tmp_primary).unlink(missing_ok=True)
                Path(tmp_secondary).unlink(missing_ok=True)
            except Exception:
                pass
        else:
            result = self._create_compilation_from_segments(segments_to_use, output_path)
            if not result.get("success"):
                console.print(f"[red]Error: {result.get('error', 'Unknown')}[/red]")
                return
            actual_path = result.get("output_path", output_path)
            segments_used = result.get("segments_used", 0)

        # Mix with music if chosen
        if music_choice:
            mixed_output = actual_path.replace(".mp3", "_with_music.mp3")
            old_path = actual_path
            mix_result = self._execute_tool_safe(
                "mix_with_background_music",
                voice_track=actual_path,
                music_track=music_choice["music_path"],
                output_path=mixed_output,
                music_volume=music_choice["volume"],
                music_fade_in=2.0,
                music_fade_out=3.0,
                music_lead_in=1.5,
                music_lead_out=2.0,
            )
            if mix_result.get("success"):
                actual_path = mix_result.get("output_path", mixed_output)
                if old_path != actual_path:
                    try:
                        Path(old_path).unlink(missing_ok=True)
                    except Exception:
                        pass

        # Optional FX
        actual_path = self._apply_fx_preset(actual_path, fx_choice)

        self.last_output = actual_path
        console.print()
        console.print(Panel.fit(
            f"[green]✓ Klaar![/green]\n\n"
            f"📁 {actual_path}\n"
            f"🎵 {segments_used} clips",
            border_style="green"
        ))

    def _create_compilation_from_segments(
        self,
        segments: List[Dict],
        output_path: str,
    ) -> Dict[str, Any]:
        """Create compilation from pre-selected segments with progress."""
        import tempfile
        
        console.print()
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(bar_width=25, complete_style="green"),
            TextColumn("[dim]{task.fields[detail]}[/dim]"),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task("Starten...", total=100, detail="")
            
            # Create temp dir
            temp_dir = Path(tempfile.gettempdir()) / f"stemmy_rpg_comp_{id(self)}"
            temp_dir.mkdir(exist_ok=True)
            
            # Extract audio using parallel extractor
            ordered_segments = self._interleave_segments(segments)
            max_segments = 200
            total_to_process = min(len(ordered_segments), max_segments)
            progress.update(task, description="🎵 Audio extracten (parallel)", completed=10, detail=f"0 van {total_to_process}")
            
            # Prepare segments for parallel extraction
            segments_to_extract = []
            for i, seg in enumerate(ordered_segments[:max_segments]):
                audio_url = (
                    seg.get("audio_url") or 
                    seg.get("item_audio_url") or 
                    seg.get("source_audio_url") or
                    seg.get("fragment_audio_url")
                )
                if audio_url:
                    # DB may return strings (e.g. '99633'); always convert to float
                    st = seg.get("start_time", seg.get("start", 0))
                    en = seg.get("end_time", seg.get("end", 0))
                    st = float(st) if st is not None else 0.0
                    en = float(en) if en is not None else 0.0
                    if en > st:
                        segments_to_extract.append({
                            'id': str(i),
                            'audio_url': audio_url,
                            'start_time': st,
                            'end_time': en,
                        })
            
            # Use parallel extractor
            from stemmy_cli.audio.parallel_extractor import ParallelExtractor
            
            extractor = ParallelExtractor(max_workers=4, output_dir=temp_dir)
            
            def update_progress(current, total, msg):
                pct = 10 + (70 * current / max(total, 1))
                progress.update(task, completed=pct, detail=f"clip {current} van {total}")
            
            successes, failures = extractor.extract_segments(
                segments_to_extract, 
                progress_callback=update_progress
            )
            
            extracted = len(successes)
            extract_results = [Path(r.output_path) for r in successes]
            
            if not extract_results:
                progress.update(task, description="[red]✗ Extractie mislukt[/red]", completed=100, detail="")
                return {"success": False, "error": "Failed to extract audio"}
            
            # Concatenate
            progress.update(task, description="🔗 Samenvoegen", completed=85, detail=f"{extracted} clips")
            
            file_list = temp_dir / "files.txt"
            with open(file_list, "w") as f:
                for seg_path in extract_results:
                    f.write(f"file '{seg_path}'\n")
            
            try:
                self._concat_audio_files(file_list, output_path)
            except Exception as e:
                return {"success": False, "error": str(e)}
            
            progress.update(task, description="[green]✓ Klaar![/green]", completed=100, detail="")
        
        # Cleanup
        try:
            for f in temp_dir.glob("*.mp3"):
                f.unlink()
            file_list.unlink()
            temp_dir.rmdir()
        except Exception:
            pass
        
        return {
            "success": True,
            "output_path": output_path,
            "segments_used": extracted,
        }
    
    # ═══════════════════════════════════════════════════════════════
    # WIZARD 4: FORMATS BROWSER
    # ═══════════════════════════════════════════════════════════════
    
    def _formats_browser(self):
        """Browse available formats."""
        console.print()
        console.print("[bold]═══ PODCASTS/FORMATS ═══[/bold]")
        console.print()
        console.print("[dim]Laden...[/dim]")
        formats = self._execute_tool_safe("list_formats", limit=25)
        if not formats:
            console.print("[yellow]Geen podcasts gevonden[/yellow]")
            return
        
        console.print()
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Titel", width=35)
        table.add_column("Taal", style="dim", width=6)
        table.add_column("Items", style="dim", width=6)
        
        for i, f in enumerate(formats, 1):
            table.add_row(
                str(i),
                f.get("title", "")[:33],
                f.get("language", "")[:4],
                str(f.get("item_count", "-"))
            )
        
        console.print(table)
        console.print()
    
    # ═══════════════════════════════════════════════════════════════
    # WIZARD 4: MUZIEK TOEVOEGEN
    # ═══════════════════════════════════════════════════════════════
    
    def _music_wizard(self):
        """Add music to existing file."""
        console.print()
        console.print("[bold]═══ MUZIEK TOEVOEGEN ═══[/bold]")
        console.print()
        
        # Check for last output
        if self.last_output and Path(self.last_output).exists():
            console.print(f"Laatste bestand: [cyan]{self.last_output}[/cyan]")
            console.print("Dit bestand gebruiken? [cyan]j[/cyan]/n")
            
            if self._prompt("Gebruiken") in ("j", "ja", "y", "yes", ""):
                self._add_music_to_file(self.last_output)
                return
        
        console.print("Welk bestand wil je muziek aan toevoegen?")
        console.print("[dim](Pad naar .mp3 bestand)[/dim]")
        console.print()
        
        filepath = self._prompt("Bestand")
        if not filepath:
            return
        
        if not Path(filepath).exists():
            console.print(f"[red]Bestand niet gevonden: {filepath}[/red]")
            return
        
        self._add_music_to_file(filepath)
    
    def _add_music_to_file(self, filepath: str):
        """Add background music to file."""
        console.print()
        console.print("[cyan]Kies muziek:[/cyan]")
        console.print()
        
        # List available music from static/music/
        from stemmy_cli.paths import get_music_dir
        music_dir = get_music_dir()
        music_files = list(music_dir.glob("*.mp3")) if music_dir.exists() else []
        
        if not music_files:
            console.print(f"[yellow]Geen muziekbestanden gevonden in {music_dir}[/yellow]")
            console.print("[dim]Plaats .mp3 bestanden in static/music/[/dim]")
            return
        
        for i, m in enumerate(music_files[:10], 1):
            console.print(f"  [cyan]{i}[/cyan]  {m.name}")
        
        console.print()
        choice = self._prompt("Nummer")
        
        if not choice.isdigit() or not (1 <= int(choice) <= len(music_files)):
            console.print("[yellow]Ongeldige keuze[/yellow]")
            return
        
        music_path = str(music_files[int(choice) - 1])
        
        # Volume
        console.print()
        console.print("[cyan]Muziek volume?[/cyan] [dim](default: 15%)[/dim]")
        vol_str = self._prompt("Volume %")
        volume = int(vol_str) if vol_str.isdigit() else 15
        volume_float = volume / 100.0
        
        # Output
        output = filepath.replace(".mp3", "_with_music.mp3")
        
        console.print()
        console.print("[dim]Mixen...[/dim]")
        
        result = self._execute_tool_safe(
            "mix_with_background_music",
            voice_track=filepath,
            music_track=music_path,
            output_path=output,
            music_volume=volume_float,
            music_fade_in=2.0,
            music_fade_out=3.0,
            music_lead_in=1.5,
            music_lead_out=2.0,
        )
        
        if result.get("success"):
            self.last_output = output
            console.print()
            console.print(Panel.fit(
                f"[green]✓ Muziek toegevoegd![/green]\n\n"
                f"📁 {output}\n"
                f"🎵 Volume: {volume}%",
                border_style="green"
            ))
        else:
            console.print(f"[red]Error: {result.get('error', 'Unknown')}[/red]")
    
    # ═══════════════════════════════════════════════════════════════
    # STATS
    # ═══════════════════════════════════════════════════════════════
    
    def _show_stats(self):
        """Show database stats."""
        
        stats = self._execute_tool_safe("get_database_stats")
        
        console.print()
        console.print("[bold]═══ DATABASE STATS ═══[/bold]")
        console.print()
        
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("", style="dim")
        table.add_column("", style="cyan bold", justify="right")
        
        for name, count in stats.items():
            table.add_row(name, f"{count:,}")
        
        console.print(table)
        console.print()


def start_rpg(debug: bool = False):
    """Start the RPG shell.
    
    Args:
        debug: Enable debug mode for troubleshooting queries
    """
    shell = RPGShell(debug=debug)
    shell.run()
