"""
Eightfold Candidate Profile Normalizer — CLI Entrypoint

Usage:
    python main.py --inputs <file_or_url> [<file_or_url> ...] [OPTIONS]

Options:
    --inputs    One or more input source paths or URLs
    --config    Path to a runtime config JSON (default: config/default_config.json)
    --output    Output file path (default: stdout)
    --github    GitHub profile URL to include as a source
    --verbose   Enable debug logging
    --no-merge  Treat each input as a separate candidate (skip merging)

Examples:
    python main.py --inputs sample_inputs/recruiter_export.csv sample_inputs/ats_blob.json
    python main.py --inputs sample_inputs/ats_blob.json --github https://github.com/jordanlee --config config/custom_config.json --output out.json
"""
from __future__ import annotations
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from pipeline.detector import detect_source_type
from pipeline.extractors.csv_extractor import extract_csv
from pipeline.extractors.json_extractor import extract_json
from pipeline.extractors.github_extractor import extract_github
from pipeline.extractors.pdf_extractor import extract_pdf
from pipeline.extractors.txt_extractor import extract_txt
from pipeline.merger import merge_candidates
from pipeline.projector import project
from pipeline.validator import validate_output


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=level,
        stream=sys.stderr,
    )


def load_config(config_path: str) -> dict:
    """Load and parse a runtime config JSON file."""
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        click.echo(f"[ERROR] Config file not found: {config_path}", err=True)
        sys.exit(1)
    except json.JSONDecodeError as e:
        click.echo(f"[ERROR] Invalid JSON in config: {e}", err=True)
        sys.exit(1)


def extract_from_source(source: str) -> list[dict]:
    """
    Detect source type and dispatch to the appropriate extractor.
    Returns empty list on any failure — never crashes.
    """
    source_type = detect_source_type(source)
    click.echo(f"  → Detected: {source!r} as [{source_type}]", err=True)

    extractors = {
        "csv": extract_csv,
        "json": extract_json,
        "github_url": extract_github,
        "pdf": extract_pdf,
        "txt": extract_txt,
    }

    extractor = extractors.get(source_type)
    if not extractor:
        click.echo(
            f"  [WARN] No extractor available for source type '{source_type}': {source}",
            err=True,
        )
        return []

    try:
        results = extractor(source)
        click.echo(f"  → Extracted {len(results)} candidate record(s)", err=True)
        return results
    except Exception as e:
        click.echo(f"  [WARN] Extraction failed for {source}: {e}", err=True)
        return []


def run_pipeline(
    inputs: list[str],
    config_path: str,
    output_path: Optional[str],
    no_merge: bool,
) -> None:
    """Core pipeline execution."""
    config = load_config(config_path)

    # ── Step 1: Extract from all sources ─────────────────────
    click.echo("\n[1/4] Extracting from sources...", err=True)
    all_records: list[dict] = []
    for source in inputs:
        records = extract_from_source(source.strip())
        all_records.extend(records)

    if not all_records:
        click.echo("[WARN] No records extracted from any source. Outputting empty result.", err=True)
        result = {"error": "No records extracted", "output": None}
        _write_output(result, output_path)
        return

    click.echo(f"  Total raw records: {len(all_records)}", err=True)

    # ── Step 2: Merge ─────────────────────────────────────────
    click.echo("\n[2/4] Merging and deduplicating...", err=True)
    if no_merge:
        # Each source treated separately
        profiles = [merge_candidates([r]) for r in all_records]
    else:
        # Group all records and merge into one profile
        # (In a full system this would group by candidate identity cluster)
        profiles = [merge_candidates(all_records)]

    click.echo(f"  → Produced {len(profiles)} merged profile(s)", err=True)

    # ── Step 3: Project & Validate ────────────────────────────
    click.echo("\n[3/4] Projecting output with config...", err=True)
    results = []
    for profile in profiles:
        profile_dict = profile.model_dump()
        try:
            projected = project(profile_dict, config)
        except ValueError as e:
            click.echo(f"  [ERROR] Projection failed: {e}", err=True)
            results.append({"error": str(e)})
            continue

        click.echo("\n[4/4] Validating output...", err=True)
        validation = validate_output(projected, config)
        if not validation.is_valid:
            click.echo(f"  [WARN] Validation issues found:", err=True)
            for v in validation.violations:
                click.echo(f"    - {v}", err=True)
        else:
            click.echo("  ✓ Validation passed", err=True)

        results.append(projected)

    final_output = results[0] if len(results) == 1 else results
    _write_output(final_output, output_path)


def _write_output(data, output_path: Optional[str]) -> None:
    """Write JSON output to file or stdout."""
    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json_str)
        click.echo(f"\n✅ Output written to: {output_path}", err=True)
    else:
        click.echo(json_str)


@click.command()
@click.option(
    "--inputs", "-i",
    multiple=True,
    required=False,
    help="Input source paths or URLs (CSV, JSON, PDF, GitHub URL). Can be specified multiple times.",
)
@click.option(
    "--github", "-g",
    multiple=True,
    help="GitHub profile URL(s) to include as a source.",
)
@click.option(
    "--config", "-c",
    default="config/default_config.json",
    show_default=True,
    help="Path to runtime config JSON file.",
)
@click.option(
    "--output", "-o",
    default=None,
    help="Output file path. Defaults to stdout.",
)
@click.option(
    "--no-merge",
    is_flag=True,
    default=False,
    help="Treat each input record as a separate candidate (skip merging).",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable verbose/debug logging.",
)
def cli(inputs, github, config, output, no_merge, verbose):
    """
    \b
    Eightfold Candidate Profile Normalizer
    ───────────────────────────────────────
    Ingests candidate data from multiple sources and emits a
    clean, canonical, deduplicated JSON profile.

    \b
    Examples:
      # Default schema
      python main.py -i sample_inputs/recruiter_export.csv -i sample_inputs/ats_blob.json

      # Custom config with GitHub source
      python main.py -i sample_inputs/ats_blob.json -g https://github.com/jordanlee \\
          -c config/custom_config.json -o sample_outputs/custom_output.json
    """
    setup_logging(verbose)

    # Combine all inputs
    all_inputs = list(inputs) + list(github)

    if not all_inputs:
        click.echo(
            "[ERROR] No inputs provided. Use --inputs / -i or --github / -g to specify sources.",
            err=True,
        )
        click.echo("Run 'python main.py --help' for usage.", err=True)
        sys.exit(1)

    click.echo(f"\n🚀 Eightfold Candidate Normalizer", err=True)
    click.echo(f"   Sources: {len(all_inputs)}", err=True)
    click.echo(f"   Config:  {config}", err=True)

    run_pipeline(
        inputs=all_inputs,
        config_path=config,
        output_path=output,
        no_merge=no_merge,
    )


if __name__ == "__main__":
    cli()
