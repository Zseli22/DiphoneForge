"""Unified CLI entry point for the DiphoneForge voice builder (`python -m voicebuilder ...`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .aligner import (
    align_external_recordings,
    align_mic_takes,
    apply_pickup_recording,
    export_pickup_script,
    parse_line_list,
    write_ranges_file,
)
from .builder import build_voice_databases
from .catalog import LanguageCatalog
from .downloader import (
    ensure_mfa_env,
    ensure_mfa_model,
    ensure_wordlist,
    inspect_mfa_model_phones,
    run_doctor,
)
from .phone_map import build_phone_map
from .planner import extend_project_plan, generate_plan
from .preview import export_voice_package, install_voice_to_nvda, synthesize_preview
from .project import VoiceProject


def _cli_progress(msg: str, frac: Optional[float]) -> None:
    if frac is not None:
        print(f"[{int(round(frac * 100)):3d}%] {msg}", flush=True)
    else:
        print(f"      {msg}", flush=True)


def _run_internal_tool(tool_name: str, tool_args: list[str]) -> int:
    """Dispatch internal pipeline tools when running inside a frozen PyInstaller executable."""
    from .paths import get_app_root

    tools_dir = get_app_root() / "tools"
    if not getattr(sys, "frozen", False) and str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    old_argv = sys.argv
    sys.argv = [tool_name, *tool_args]
    try:
        if tool_name == "plan_word_corpus":
            import plan_word_corpus
            res = plan_word_corpus.main()
            return int(res) if isinstance(res, int) else 0
        if tool_name == "build_word_mbrola_db":
            import build_word_mbrola_db
            res = build_word_mbrola_db.main()
            return int(res) if isinstance(res, int) else 0
        if tool_name == "smoke_mbrola_host":
            import smoke_mbrola_host
            res = smoke_mbrola_host.main()
            return int(res) if isinstance(res, int) else 0
        print(f"Unknown internal tool: {tool_name}", file=sys.stderr)
        return 2
    finally:
        sys.argv = old_argv


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) >= 2 and raw_argv[0] == "--_run-tool":
        return _run_internal_tool(raw_argv[1], raw_argv[2:])

    parser = argparse.ArgumentParser(
        prog="python -m voicebuilder",
        description="DiphoneForge language- and speaker-neutral MBROLA voice builder CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_gui = sub.add_parser("gui", help="Launch the accessible wxPython voice builder wizard (default if no subcommand).")
    p_gui.add_argument("--project", type=Path, default=None, help="Optional existing project folder to open immediately.")

    p_pick = sub.add_parser("picker", help="Open the interactive Diphone Take Picker on a build folder.")
    p_pick.add_argument("--unit-grid", type=Path, required=True, help="Path to unit_grid.csv")
    p_pick.add_argument("--segments-csv", type=Path, required=True, help="Path to segments.csv")
    p_pick.add_argument("--audio-dir", type=Path, required=True, help="Directory containing source WAV(s)")
    p_pick.add_argument("--overrides", type=Path, default=None, help="Path to take_overrides.json (default next to unit_grid.csv)")
    p_pick.add_argument("--voice", default="en-gb", help="eSpeak voice for word/phrase diphone filtering")
    p_pick.add_argument("--inventory", type=Path, default=None, help="Optional inventory.json for exact phone normalization")

    sub.add_parser("languages", help="List all supported languages in data/language_catalog.json.")

    p_doc = sub.add_parser("doctor", help="Check bundled tools, MFA runtime, and portable cache.")
    p_doc.add_argument("--clean-slate", action="store_true", help="Ignore external MFA env to simulate first-time user.")

    p_init = sub.add_parser("init", help="Create a new voice project folder (`project.json`).")
    p_init.add_argument("--project", type=Path, required=True)
    p_init.add_argument("--lang", required=True, help="Language ID from `languages` (e.g. en-gb, es, hu, de).")
    p_init.add_argument("--voice-id", required=True, help="Voice DB identifier (e.g. ukd16, esd16).")
    p_init.add_argument("--rates", default="22050", help="Comma-separated sample rates (22050, 16000, or 22050,16000).")
    p_init.add_argument("--mode", choices=["mic", "external"], default="mic")
    p_init.add_argument("--wordlist", default="auto", help="'auto' (FrequencyWords) or path to custom text/wordlist.")
    p_init.add_argument("--max-words", type=int, default=500)

    p_plan = sub.add_parser("plan", help="Download word list (if auto) and generate hybrid recording script.")
    p_plan.add_argument("--project", type=Path, required=True)

    p_ext = sub.add_parser("extend", help="Append extra real words to an existing plan for more diphone takes.")
    p_ext.add_argument("--project", type=Path, required=True)
    p_ext.add_argument("--extra-words", type=int, default=200)
    p_ext.add_argument("--min-takes", type=int, default=2)

    p_map = sub.add_parser("check-phonemap", help="Download MFA model on demand and verify eSpeak->MFA phone map.")
    p_map.add_argument("--project", type=Path, required=True)

    p_align = sub.add_parser("align", help="Run MFA alignment & audit (auto-downloads model if needed).")
    p_align.add_argument("--project", type=Path, required=True)
    p_align.add_argument("--jobs", type=int, default=4)

    p_pick_exp = sub.add_parser("pickup-export", help="Export a pickup script for ONLY specific flagged line numbers.")
    p_pick_exp.add_argument("--project", type=Path, required=True)
    p_pick_exp.add_argument("--lines", required=True, help="Comma/range list of line numbers, e.g. '142,319,410-412'.")

    p_pick_app = sub.add_parser("pickup-apply", help="Align a partial external pickup WAV and patch ONLY those lines.")
    p_pick_app.add_argument("--project", type=Path, required=True)
    p_pick_app.add_argument("--wav", type=Path, required=True)
    p_pick_app.add_argument("--lines", required=True, help="Ordered line numbers recorded in --wav.")

    p_build = sub.add_parser("build", help="Build validated MBROLA database(s) and eSpeak voice tables.")
    p_build.add_argument("--project", type=Path, required=True)
    p_build.add_argument("--jobs", type=int, default=None)

    p_prev = sub.add_parser("preview", help="Synthesize a test WAV using the built voice.")
    p_prev.add_argument("--project", type=Path, required=True)
    p_prev.add_argument("--db-name", default=None)
    p_prev.add_argument("--text", default=None)
    p_prev.add_argument("--out", type=Path, default=None)

    p_exp = sub.add_parser("export", help="Export standalone voice package folder.")
    p_exp.add_argument("--project", type=Path, required=True)
    p_exp.add_argument("--db-name", required=True)
    p_exp.add_argument("--dest", type=Path, required=True)

    p_inst = sub.add_parser("install-nvda", help="Install built voice into local NVDA espeakWitMbrola add-on.")
    p_inst.add_argument("--project", type=Path, required=True)
    p_inst.add_argument("--db-name", required=True)

    p_boot = sub.add_parser("bootstrap-mfa", help="Download micromamba + install MFA environment into portable cache.")
    p_boot.add_argument("--clean-slate", action="store_true", help="Install fresh into <cache>/mfa-env even if external MFA exists.")

    args = parser.parse_args(argv)
    if args.cmd in (None, "gui"):
        from .gui.app import launch_gui
        return launch_gui(initial_project=getattr(args, "project", None))

    if args.cmd == "picker":
        from .gui.take_picker import launch_standalone_picker
        ov = args.overrides or (args.unit_grid.parent / "take_overrides.json")
        return launch_standalone_picker(
            unit_grid_csv=args.unit_grid,
            segments_csv=args.segments_csv,
            audio_dir=args.audio_dir,
            overrides_json=ov,
            voice=args.voice,
            inventory_json=args.inventory,
        )

    cat = LanguageCatalog()

    if args.cmd == "languages":
        for spec in cat.all():
            print(f"{spec.id:8s}  {spec.mfa_tier:3s}  {spec.mfa_model:18s}  wl={str(spec.wordlist_code or 'custom'):6s}  {spec.name}")
        return 0

    if args.cmd == "doctor":
        for st in run_doctor(ignore_external_mfa=args.clean_slate):
            mark = "OK  " if st.ok else "MISS"
            print(f"[{mark}] {st.name:34s}  {st.detail}")
        return 0

    if args.cmd == "bootstrap-mfa":
        env_dir = ensure_mfa_env(progress=_cli_progress, ignore_external=args.clean_slate)
        print(f"MFA ready: {env_dir}")
        return 0

    if args.cmd == "init":
        lang = cat.get(args.lang)
        rates = [int(x.strip()) for x in args.rates.split(",") if x.strip()]
        proj = VoiceProject.create(
            args.project,
            args.voice_id,
            lang,
            rates=rates,
            wordlist_source=args.wordlist,
            recording_mode=args.mode,
            max_words=args.max_words,
        )
        print(f"Created project at {proj.root} (voice={proj.config.voice_id}, lang={lang.id}, mfa={lang.mfa_model})")
        return 0

    proj = VoiceProject.load(args.project)

    if args.cmd == "plan":
        summary = generate_plan(proj, catalog=cat, progress=_cli_progress)
        print(json.dumps({
            "voice": summary.voice,
            "phones": summary.vowel_count + summary.consonant_count,
            "words": summary.word_count,
            "supplements": summary.supplement_count,
            "total_prompts": summary.total_prompts,
            "running_speech_pct": summary.running_speech_pct,
            "wordlist": str(summary.wordlist_path),
        }, indent=2))
        return 0

    if args.cmd == "extend":
        ext = extend_project_plan(
            proj,
            extra_words=args.extra_words,
            min_takes=args.min_takes,
            catalog=cat,
            progress=_cli_progress,
        )
        print(json.dumps({
            "batch_id": ext.batch_id,
            "added_words": ext.added_words,
            "first_line": ext.first_line,
            "last_line": ext.last_line,
            "total_prompts": ext.total_prompts,
            "script_path": str(ext.script_path),
        }, indent=2))
        return 0

    if args.cmd == "check-phonemap":
        phones = inspect_mfa_model_phones(proj.config.mfa_model, progress=_cli_progress)
        inv = json.loads((proj.plan_dir / "inventory.json").read_text(encoding="utf-8"))
        pm = build_phone_map(inv, proj.config.mfa_model, phones)
        print(f"Model {proj.config.mfa_model}: {len(phones)} model phones | {len(pm.exact)} exact, {len(pm.approximate)} approximate")
        for ph, ipa, mapped in pm.approximate:
            print(f"  approx: {ph:4s} (ipa={ipa}) -> {mapped}")
        return 0

    if args.cmd == "align":
        if proj.config.recording_mode == "external":
            rep = align_external_recordings(proj, num_jobs_pass2=args.jobs, progress=_cli_progress)
        else:
            rep = align_mic_takes(proj, num_jobs=args.jobs, progress=_cli_progress)
        print(f"Aligned {rep.aligned_lines}/{rep.total_lines} lines ({rep.phone_ok_lines} exact phone counts), {len(rep.flags)} flagged.")
        for f in rep.flags[:20]:
            print(f"  FLAG line {f.line:04d} ({f.text}): {', '.join(f.reasons)}")
        return 0

    if args.cmd == "pickup-export":
        lines = parse_line_list(args.lines)
        bid, script_p, _ = export_pickup_script(proj, lines)
        print(f"Exported pickup batch {bid} ({len(lines)} lines) -> {script_p}")
        return 0

    if args.cmd == "pickup-apply":
        lines = parse_line_list(args.lines)
        rep = apply_pickup_recording(proj, args.wav, lines, progress=_cli_progress)
        print(f"Applied pickup ({len(lines)} lines). Remaining flags: {len(rep.flags)}")
        return 0

    if args.cmd == "build":
        summaries = build_voice_databases(proj, jobs=args.jobs, progress=_cli_progress)
        print(json.dumps(summaries, indent=2))
        return 0

    if args.cmd == "preview":
        db_name = args.db_name or proj.config.voice_id
        text = args.text or proj.language_spec(cat).preview_text or "Hello world."
        wav = synthesize_preview(proj, db_name, text, out_wav=args.out)
        print(f"Rendered preview WAV: {wav}")
        return 0

    if args.cmd == "export":
        dest = export_voice_package(proj, args.db_name, args.dest)
        print(f"Exported voice package to {dest}")
        return 0

    if args.cmd == "install-nvda":
        dest = install_voice_to_nvda(proj, args.db_name)
        print(f"Installed {args.db_name} into {dest}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
