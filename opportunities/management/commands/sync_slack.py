"""Preview/import configured Slack channels through a local browser session."""
from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from opportunities.services.importing import import_candidates
from opportunities.services.slack_browser import SlackImportError, login_slack, scrape_slack
from opportunities.services.slack_source import SOURCE_KEY, get_channel, get_channels


def _checkpoint_path():
    return Path(settings.BASE_DIR) / ".slack-sync-state.json"


def _checkpoint_entries():
    """Read state without letting one invalid timestamp hide other channels."""
    path = _checkpoint_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("version") == 2:
            entries = payload["channels"]
            if not isinstance(entries, dict):
                raise ValueError
            return entries
        # The former format belongs exclusively to the original source, even
        # when that source is no longer the first configured channel.
        if "version" in payload or payload.get("source") != SOURCE_KEY:
            raise ValueError
        return {SOURCE_KEY: payload["latest_ts"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CommandError("Slack checkpoint could not be read. Use --rescan for a full requested window.") from exc


def _valid_timestamp(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{10}\.[0-9]{6}", value) is not None


def read_checkpoint(channel=None):
    source_key = get_channel(channel).source_key
    entries = _checkpoint_entries()
    if source_key not in entries:
        return None
    value = entries[source_key]
    if not _valid_timestamp(value):
        raise CommandError("Slack checkpoint could not be read. Use --rescan for a full requested window.")
    return value


def write_checkpoint(timestamp, channel=None):
    channel = get_channel(channel)
    if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", timestamp):
        raise CommandError("Refusing an invalid Slack checkpoint.")
    try:
        entries = _checkpoint_entries()
    except CommandError:
        entries = {}  # A successful --rescan repairs invalid file structure.
    previous = entries.get(channel.source_key)
    now = Decimal(str(datetime.now(timezone.utc).timestamp()))
    if _valid_timestamp(previous) and Decimal(timestamp) <= Decimal(previous) <= now:
        timestamp = previous
    entries[channel.source_key] = timestamp
    path = _checkpoint_path()
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"version": 2, "channels": entries}) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise CommandError("Imports were saved, but the Slack checkpoint could not be written; repeats remain deduplicated.") from exc


class Command(BaseCommand):
    help = "Sign in, preview, or import announcements from configured Slack channels as drafts."

    def add_arguments(self, parser):
        parser.add_argument("--import", action="store_true", dest="commit", help="Save drafts/incomplete items; default is preview only.")
        parser.add_argument("--login", action="store_true", help="Open a separate browser for interactive sign-in only.")
        parser.add_argument("--headed", action="store_true", help="Show the importer browser while scanning.")
        parser.add_argument("--days", type=int, default=30, help="Initial/full scan window, 1-365 days (default 30).")
        parser.add_argument("--limit", type=int, help="Read an explicit newest batch of 1-1000 messages without advancing the checkpoint.")
        parser.add_argument("--browser", choices=("chrome", "msedge", "chromium"), default="chrome", dest="browser_channel")
        parser.add_argument("--rescan", action="store_true", help="Ignore the checkpoint and read the full --days window again.")
        parser.add_argument(
            "--channel", action="append", dest="channels",
            help="Select a configured channel by URL, channel ID, or source key; repeat to select several. Default: all configured channels.",
        )
        parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON instead of a human report.")

    def handle(self, *args, **options):
        days, limit = options["days"], options["limit"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise CommandError("--days must be an integer between 1 and 365.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000):
            raise CommandError("--limit must be an integer between 1 and 1000.")
        if options["browser_channel"] not in {"chrome", "msedge", "chromium"}:
            raise CommandError("--browser must be chrome, msedge, or chromium.")
        if options["login"] and options["commit"]:
            raise CommandError("--login and --import cannot be combined. Sign in first, then scan.")
        try:
            configured = get_channels()
            selections = options.get("channels")
            if selections is not None and not isinstance(selections, (list, tuple)):
                raise ValueError("--channel selections must be provided as a list.")
            channels = []
            for value in selections or configured:
                channel = get_channel(value)
                if channel not in channels:
                    channels.append(channel)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if options["login"]:
            if selections and len(channels) != 1:
                raise CommandError("Use --login with one --channel at a time.")
            channel = channels[0]
            if not options["as_json"]:
                self.stdout.write(f"Sign in normally in the separate browser: {channel.url}")
                self.stdout.flush()
            try:
                login_slack(browser_channel=options["browser_channel"], channel=channel)
            except (SlackImportError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(
                json.dumps({"mode": "login", "source": channel.source_key, "authenticated": True})
                if options["as_json"] else "Sign-in complete. The local importer profile is ready."
            )
            return

        reports = []
        unexpected_error = None
        for channel in channels:
            try:
                reports.append(self.scan_channel(channel, options))
            except Exception as exc:
                # Preserve programming errors while letting another configured
                # channel finish its independent scan before re-raising.
                if len(channels) == 1:
                    raise
                unexpected_error = unexpected_error or exc
                payload = self.empty_report(channel, options)
                payload["error"] = "Unexpected scan failure; see the command traceback or local log."
                reports.append(payload)
        payload = reports[0] if len(reports) == 1 else {
            "mode": "import" if options["commit"] else "preview",
            "complete": all(report["complete"] and not report.get("error") for report in reports),
            "channels": reports,
        }
        self.write_report(payload, as_json=options["as_json"])
        if unexpected_error is not None:
            raise unexpected_error
        failures = [report for report in reports if report.get("error")]
        if failures:
            if len(reports) == 1:
                raise CommandError(failures[0]["error"])
            raise CommandError("Some Slack channels failed: " + "; ".join(
                f"{report['source']}: {report['error']}" for report in failures
            ))

    @staticmethod
    def empty_report(channel, options):
        return {
            "mode": "import" if options["commit"] else "preview", "source": channel.source_key,
            "channel_url": channel.url, "complete": False, "discovered": 0,
            "skipped": 0, "collected": 0, "warnings": [], "items": [],
            "limit": options["limit"], "days": options["days"],
            "window_start": "", "latest_ts": None,
        }

    def scan_channel(self, channel, options):
        payload = self.empty_report(channel, options)
        try:
            since = read_checkpoint(channel=channel) if options["commit"] and not options["rescan"] else None
            browser_options = {
                "days": options["days"], "limit": options["limit"], "browser_channel": options["browser_channel"],
                "headed": options["headed"], "since": since, "channel": channel,
            }
            if options["verbosity"] >= 2:
                browser_options["progress"] = self.stderr.write
            result = scrape_slack(**browser_options)
            payload.update({
                "complete": result.complete, "discovered": result.discovered,
                "skipped": result.skipped, "collected": len(result.items),
                "warnings": result.warnings, "window_start": result.window_start,
                "latest_ts": result.latest_ts, "items": result.items,
            })
            if not result.complete:
                payload["error"] = "Collection is incomplete. Nothing was imported for this channel and its checkpoint was preserved."
                return payload
            if options["commit"]:
                payload["summary"] = import_candidates(result.items, source_key=channel.source_key, source="slack")
                if payload["summary"]["errors"]:
                    payload["error"] = "Some messages could not be imported. This channel's checkpoint was preserved; see the report."
                elif options["limit"] is None and result.latest_ts:
                    write_checkpoint(result.latest_ts, channel=channel)
        except (SlackImportError, ValueError, CommandError) as exc:
            payload["error"] = str(exc)
        return payload

    def write_report(self, payload, *, as_json):
        if as_json:
            self.stdout.write(json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
            return
        if "channels" in payload:
            for report in payload["channels"]:
                self.write_report(report, as_json=False)
            return
        self.stdout.write(f"Channel: {payload['channel_url']}")
        self.stdout.write(
            f"Slack {payload['mode']}: {payload['discovered']} messages read; "
            f"{payload['collected']} opportunity candidates; {payload['skipped']} messages not classified as opportunities; "
            + ("complete." if payload["complete"] else "INCOMPLETE.")
        )
        if payload["mode"] == "preview":
            self.stdout.write("Preview only: no database records or checkpoint were changed.")
        for warning in payload["warnings"]:
            self.stdout.write(f"Warning: {warning}")
        for item in payload["items"]:
            self.stdout.write(
                f"- {item.get('title') or '[Needs title]'} | "
                f"{item.get('organization') or '[Needs organization]'} | "
                f"{item.get('type') or '[Needs type]'}\n  {item['source_url']}"
            )
        if "summary" in payload:
            summary = payload["summary"]
            self.stdout.write(
                f"Saved: {summary['created']} drafts; {summary['staged']} staged; "
                f"{summary['unchanged']} unchanged; {summary['changed']} source changes flagged; "
                f"{summary['errors']} errors."
            )
            for message in summary.get("messages", []):
                self.stdout.write(message)
        if payload.get("error"):
            self.stdout.write(f"Error: {payload['error']}")
