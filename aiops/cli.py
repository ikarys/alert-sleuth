from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from aiops import analysis, catalog, db, ingest, quality, triage
from aiops.config import load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aiops",
        description="Assistant AIOps en lecture seule",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("fetch", help="Récupérer les alertes")

    triage_parser = subparsers.add_parser("triage", help="Lancer le triage")
    triage_parser.add_argument("--no-llm", action="store_true", help="Désactiver le raffinement LLM")

    list_parser = subparsers.add_parser("list", help="Lister les groupes")
    list_parser.add_argument("--service", help="Filtrer par service")
    list_parser.add_argument("--decision", help="Filtrer par décision")

    analyze_parser = subparsers.add_parser("analyze", help="Analyser un groupe")
    analyze_parser.add_argument("group_id", help="Identifiant du groupe")

    report_parser = subparsers.add_parser("report", help="Afficher un rapport")
    report_parser.add_argument("report_id", help="Identifiant du rapport")
    report_parser.add_argument("--json", action="store_true", help="Afficher le rapport en JSON")

    subparsers.add_parser("quality", help="Afficher la qualité des données")

    web_parser = subparsers.add_parser("web", help="Démarrer le serveur web")
    web_parser.add_argument("--host", default="127.0.0.1", help="Hôte d'écoute")
    web_parser.add_argument("--port", type=int, default=8080, help="Port d'écoute")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 1

    settings = load()
    conn = db.connect(settings.db_path)

    try:
        if args.command == "fetch":
            count = ingest.fetch(settings, conn)
            print(f"{count} alertes récupérées")

        elif args.command == "triage":
            groups = triage.run(settings, conn, use_llm=not args.no_llm)
            print(f"Triage terminé : {len(groups)} groupes")

        elif args.command == "list":
            groups = db.list_groups(conn, service=args.service, decision=args.decision)
            _print_groups(groups)

        elif args.command == "analyze":
            def on_step(name: str, status: str, detail: str) -> None:
                print(f"[{status}] {name} : {detail}")

            report_id = analysis.analyze(settings, conn, args.group_id, on_step=on_step)
            print(f"Rapport {report_id}")

        elif args.command == "report":
            try:
                report_id = int(args.report_id)
            except ValueError as exc:
                print(f"Identifiant de rapport invalide : {args.report_id}", file=sys.stderr)
                return 1

            report = db.get_report(conn, report_id)
            if report is None:
                print(f"Rapport {report_id} introuvable", file=sys.stderr)
                return 1

            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(report.get("markdown") or "")

        elif args.command == "quality":
            entries = catalog.load(settings.catalog_path)
            rows = quality.scorecard(conn, entries)
            _print_quality(rows)

        elif args.command == "web":
            import uvicorn

            from aiops.web.app import create_app

            uvicorn.run(create_app(settings), host=args.host, port=args.port)

        else:
            parser.print_help(sys.stderr)
            return 1

    except Exception as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


def _print_groups(groups: list[dict]) -> None:
    if not groups:
        print("Aucun groupe")
        return

    headers = [
        "id",
        "service",
        "env",
        "priority",
        "count",
        "score",
        "adj",
        "final",
        "decision",
        "reason",
    ]
    rows = [
        [
            group.get("id"),
            group.get("service"),
            group.get("env"),
            group.get("priority"),
            group.get("count"),
            group.get("score"),
            group.get("adjustment"),
            group.get("final_score"),
            group.get("decision"),
            group.get("reason"),
        ]
        for group in groups
    ]
    _print_table(headers, rows)


def _print_quality(rows: list[dict]) -> None:
    headers = ["indicateur", "valeur", "objectif", "statut"]
    table_rows: list[list[Any]] = []

    for row in rows:
        value = row.get("value")
        value_text = "n/a" if value is None else f"{value:.2f}%"

        target = row.get("target")
        if isinstance(target, (int, float)):
            target_text = f"{target:.0f}%"
        else:
            target_text = str(target)

        table_rows.append(
            [
                row.get("indicator"),
                value_text,
                target_text,
                row.get("status"),
            ]
        )

    _print_table(headers, table_rows)


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    str_rows = [
        ["" if cell is None else str(cell) for cell in row]
        for row in rows
    ]

    widths = [len(header) for header in headers]
    for row in str_rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))

    def format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))

    print(format_row(headers))
    print("  ".join("-" * width for width in widths))
    for row in str_rows:
        print(format_row(row))


if __name__ == "__main__":
    raise SystemExit(main())
