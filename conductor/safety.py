"""Severity-based approvals: rate every action, auto-allow the low-risk ones.

Levels (lowest to highest):
  safe      read-only: listing, reading, searching, opening a page
  low       small, reversible changes inside the workspace
  medium    installs software, runs code, deletes workspace files, fills forms
  high      outside the workspace or outward-facing: sudo, git push, sending data,
            reading secrets, clicking buy/send/delete
  critical  destructive or system-level: wiping disks, rm -rf on home/root,
            piping downloads into a shell. Always asks, whatever the policy.

The policy auto-allows everything up to a threshold and asks above it (or
declines, for unattended runs). "Always allow" rules remember a command prefix
together with the level it was approved at, so they never cover a riskier
command that happens to start the same way.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

LEVELS = ["safe", "low", "medium", "high", "critical"]


def rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else LEVELS.index("medium")


@dataclass
class Assessment:
    level: str
    reason: str
    sure: bool = True  # False -> ask the judge model

    def to_dict(self) -> dict:
        return {"level": self.level, "reason": self.reason}


# ------------------------------------------------------------------ shell rules
CRITICAL_PATTERNS = [
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "pipes a download straight into a shell"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\b.*\bof=/dev/", "writes raw data to a device"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "overwrites a disk device"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\brm\s+(-\w*\s+)*(-\w*[rf]\w*\s+)+(-\w*\s+)*(/|~|\$HOME|/\*|~/\*|\.\.)(\s|$)", "recursively deletes home or root"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)", "opens permissions on the whole system"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shuts down or restarts the machine"),
    (r"\bgit\s+push\b.*(--force|-f)\b.*\b(main|master)\b", "force-pushes a main branch"),
]

HIGH_COMMANDS = {
    "sudo": "runs as administrator", "su": "switches user", "doas": "runs as administrator",
    "ssh": "connects to another machine", "scp": "copies to/from another machine",
    "rsync": "syncs files, possibly to another machine", "systemctl": "changes system services",
    "service": "changes system services", "crontab": "changes scheduled system jobs",
    "kill": "stops processes", "pkill": "stops processes", "killall": "stops processes",
    "mail": "sends email", "sendmail": "sends email", "mount": "mounts filesystems",
    "umount": "unmounts filesystems", "useradd": "changes system users", "passwd": "changes passwords",
    "iptables": "changes the firewall", "ufw": "changes the firewall",
}
HIGH_SUBCOMMANDS = [
    (("git", "push"), "pushes code to a remote"),
    (("npm", "publish"), "publishes a package"), (("twine", "upload"), "publishes a package"),
    (("gh", "pr"), "acts on GitHub"), (("gh", "release"), "publishes a release"),
    (("docker", "push"), "publishes an image"), (("apt", "remove"), "removes system packages"),
    (("apt-get", "remove"), "removes system packages"), (("brew", "uninstall"), "removes system packages"),
]
MEDIUM_COMMANDS = {
    "python": "runs code", "python3": "runs code", "node": "runs code", "deno": "runs code", "bun": "runs code",
    "bash": "runs a script", "sh": "runs a script", "zsh": "runs a script", "make": "runs a build",
    "cargo": "builds/runs code", "go": "builds/runs code", "java": "runs code", "ruby": "runs code",
    "perl": "runs code", "npx": "downloads and runs a package", "uvx": "downloads and runs a package",
    "docker": "runs containers", "pytest": "runs tests", "rm": "deletes files", "rmdir": "deletes folders",
    "curl": "makes a network request", "wget": "downloads files", "chmod": "changes permissions",
    "chown": "changes ownership", "playwright": "runs a browser tool",
}
MEDIUM_SUBCOMMANDS = [
    (("pip", "install"), "installs packages"), (("pip3", "install"), "installs packages"),
    (("npm", "install"), "installs packages"), (("npm", "i"), "installs packages"), (("npm", "ci"), "installs packages"),
    (("npm", "run"), "runs a project script"), (("npm", "test"), "runs tests"), (("yarn", "add"), "installs packages"),
    (("pnpm", "add"), "installs packages"), (("uv", "pip"), "installs packages"), (("uv", "add"), "installs packages"),
    (("apt", "install"), "installs system packages"), (("apt-get", "install"), "installs system packages"),
    (("brew", "install"), "installs system packages"), (("git", "reset"), "rewrites git state"),
    (("git", "checkout"), "switches git state"), (("git", "commit"), "commits changes"),
    (("git", "rebase"), "rewrites git history"), (("git", "clean"), "deletes untracked files"),
    (("git", "pull"), "pulls remote changes"), (("git", "merge"), "merges branches"),
]
LOW_COMMANDS = {
    "mkdir": "creates folders", "touch": "creates files", "cp": "copies files", "mv": "moves files",
    "ln": "creates links", "tar": "packs/unpacks archives", "zip": "creates archives",
    "unzip": "unpacks archives", "gzip": "compresses files", "gunzip": "decompresses files",
    "tee": "writes files", "sed": "edits text", "awk": "processes text", "cd": "changes directory",
    "export": "sets a variable", "source": "loads settings", "true": "no-op", "sleep": "waits",
}
LOW_SUBCOMMANDS = [
    (("git", "add"), "stages files"), (("git", "init"), "creates a repo"), (("git", "clone"), "downloads a repo"),
    (("git", "stash"), "stashes changes"), (("git", "fetch"), "fetches remote refs"), (("git", "switch"), "switches branch"),
    (("git", "tag"), "tags a commit"), (("git", "config"), "changes git settings"), (("npm", "init"), "creates a project"),
]
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "rg", "egrep", "fgrep", "wc", "pwd", "echo", "printf",
    "which", "whereis", "type", "file", "stat", "du", "df", "date", "whoami", "id", "uname", "hostname",
    "tree", "sort", "uniq", "cut", "tr", "jq", "yq", "diff", "cmp", "md5sum", "sha256sum", "basename",
    "dirname", "realpath", "readlink", "column", "nl", "seq", "test", "[", "env", "printenv", "uptime",
    "free", "ps", "top", "lsof", "nproc", "find", "fd", "xxd", "hexdump", "od", "strings", "man", "help",
}
SAFE_SUBCOMMANDS = [
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"), ("git", "branch"), ("git", "remote"),
    ("git", "rev-parse"), ("git", "ls-files"), ("git", "blame"), ("pip", "list"), ("pip", "show"),
    ("pip", "freeze"), ("pip3", "list"), ("npm", "ls"), ("npm", "list"), ("npm", "view"), ("npm", "outdated"),
    ("go", "version"), ("go", "env"), ("go", "list"), ("go", "vet"), ("docker", "ps"), ("docker", "images"),
]
SECRET_PATHS = re.compile(r"(~|\$HOME|/home/[^/\s]+|/root)/\.(ssh|aws|gnupg|config/gh|netrc|docker/config)"
                          r"|/etc/(shadow|sudoers)|\bid_(rsa|ed25519)\b")
VERSION_FLAGS = {"--version", "-V", "--help", "-h", "version"}


def _segments(cmd: str) -> list[str]:
    return [s.strip() for s in re.split(r"\|\||&&|;|\||\n|\$\(|`", cmd) if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        toks = shlex.split(segment, comments=True)
    except ValueError:
        toks = segment.split()
    while toks and re.match(r"^\w+=", toks[0]):  # leading VAR=value
        toks = toks[1:]
    return toks


def _outside(path: str, workspace: Path) -> bool:
    if path.startswith(("~", "$HOME")):
        return True
    if path.startswith("/"):
        if path.startswith(("/dev/null", "/tmp/", "/dev/stdout", "/dev/stderr")) or path == "/tmp":
            return False
        try:
            return not Path(path).resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            return True
    if ".." in Path(path).parts:
        try:
            return not (workspace / path).resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            return True
    return False


GH_READ = {"list", "view", "status", "diff", "checks", "search", "browse"}


def _classify_gh(toks: list[str], seg: str) -> tuple[str, str, bool]:
    """GitHub CLI: looking is safe, cloning is low, changing anything on GitHub is high."""
    group = toks[1] if len(toks) > 1 else ""
    action = toks[2] if len(toks) > 2 else ""
    if group in ("auth", "status", "search", "--version", "version") and action in ("", "status", "repos", "issues", "prs", "code", "commits"):
        return "safe", "Reads from GitHub.", True
    if group == "api":
        if re.search(r"(-X|--method)\s*(POST|PUT|PATCH|DELETE)|\s-(f|F)\s|--field|--raw-field|--input", seg):
            return "high", "It changes something on GitHub through the API.", True
        return "safe", "Reads from the GitHub API.", True
    if action in GH_READ:
        return "safe", "Reads from GitHub.", True
    if group == "repo" and action == "clone":
        return "low", "Downloads a repo.", True
    return "high", f"It changes something on GitHub ({group} {action}).".replace(" )", ")"), True


def _classify_segment(seg: str, toks: list[str], workspace: Path) -> tuple[str, str, bool]:
    exe = Path(toks[0]).name
    if exe == "gh":
        return _classify_gh(toks, seg)
    sub = toks[1] if len(toks) > 1 else ""
    args = toks[1:]
    outside_args = [a for a in args if not a.startswith("-") and ("/" in a or a.startswith("~"))
                    and _outside(a, workspace)]
    if exe in HIGH_COMMANDS:
        return "high", f"It {HIGH_COMMANDS[exe]}.", True
    hit = next((why for (e, s), why in HIGH_SUBCOMMANDS if e == exe and s == sub), None)
    if hit:
        return "high", f"It {hit}.", True
    if exe in ("curl", "wget") and re.search(r"(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s|--upload-file|-T\s|-F\s|--form)", seg):
        return "high", "It sends data to a website.", True
    if (exe, sub) in SAFE_SUBCOMMANDS or (exe in SAFE_COMMANDS and not (exe == "find" and re.search(r"-delete|-exec", seg))) \
            or (sub in VERSION_FLAGS and len(toks) == 2):
        if outside_args and exe not in ("which", "type", "echo", "printf"):
            return "low", "Reads files outside the workspace.", True
        return "safe", "Read-only.", True
    if exe == "find":
        return "medium", "find with -delete/-exec changes or runs things.", True
    hit = next((why for (e, s), why in MEDIUM_SUBCOMMANDS if e == exe and s == sub), None)
    if hit:
        return ("high", f"It {hit} outside the workspace.", True) if outside_args else ("medium", f"It {hit}.", True)
    hit = next((why for (e, s), why in LOW_SUBCOMMANDS if e == exe and s == sub), None)
    if hit:
        return "low", f"It {hit}.", True
    if exe in MEDIUM_COMMANDS:
        if outside_args and exe in ("rm", "rmdir", "chmod", "chown"):
            return "high", f"It {MEDIUM_COMMANDS[exe]} outside the workspace.", True
        return "medium", f"It {MEDIUM_COMMANDS[exe]}.", True
    if exe in LOW_COMMANDS:
        if exe == "cd":
            return "safe", "Changes directory.", True
        if outside_args and exe not in ("source", "export"):
            return "high", f"It {LOW_COMMANDS[exe]} outside the workspace.", True
        return "low", f"It {LOW_COMMANDS[exe]}.", True
    return "medium", f"Unrecognized command '{exe}'.", False


def assess_shell(cmd: str, workspace: Path) -> Assessment:
    text = cmd.strip()
    for rx, why in CRITICAL_PATTERNS:
        if re.search(rx, text):
            return Assessment("critical", f"This {why}.")
    if SECRET_PATHS.search(text):
        return Assessment("high", "It touches credentials (SSH keys, cloud or system secrets).")

    worst = Assessment("safe", "Read-only.")
    unsure = False

    def bump(level: str, reason: str) -> None:
        nonlocal worst
        if rank(level) > rank(worst.level):
            worst = Assessment(level, reason)

    # output redirection
    for m in re.finditer(r"(?<![0-9&])>>?\s*([^\s;&|]+)", text):
        target = m.group(1)
        if target.startswith("&"):
            continue
        if _outside(target, workspace):
            bump("high", f"Writes to {target}, outside the workspace.")
        else:
            bump("low", "Writes a file in the workspace.")

    escaped = False  # a `cd` out of the workspace makes later changes count as outside
    for seg in _segments(text):
        toks = _tokens(seg)
        if not toks:
            continue
        level, reason, sure = _classify_segment(seg, toks, workspace)
        if Path(toks[0]).name == "cd" and len(toks) > 1 and _outside(toks[1], workspace):
            escaped = True
        elif escaped and rank(level) >= rank("low") and rank(level) < rank("high"):
            level, reason = "high", f"{reason.rstrip('.')} after leaving the workspace."
        unsure = unsure or not sure
        bump(level, reason)

    worst.sure = not unsure
    return worst


# ------------------------------------------------------------------ browser rules
RISKY_CLICK = re.compile(
    r"\b(buy|purchase|order|checkout|check out|pay|place order|subscribe|donate|transfer|send|post|publish|"
    r"submit|confirm|delete|remove|cancel (my )?(account|subscription)|unsubscribe|sign up|register|book now|reserve|"
    r"accept( all)?|agree|authorize|allow)\b", re.I)
SENSITIVE_URL = re.compile(r"(checkout|payment|billing|cart|/pay\b|bank|wallet|account/(delete|close)|settings/security)", re.I)
SENSITIVE_FIELD = re.compile(r"(password|passwd|card|cvv|cvc|ssn|iban|routing|otp|2fa|security.?code)", re.I)


def assess_browser(args: dict, current_url: str = "") -> Assessment:
    action = args.get("action", "")
    target = " ".join(str(args.get(k, "")) for k in ("text", "selector"))
    if action in ("goto", "read", "screenshot", "scroll", "back", "close"):
        return Assessment("safe", "Just looking.")
    on_sensitive = bool(SENSITIVE_URL.search(current_url or ""))
    if action == "click":
        if RISKY_CLICK.search(target):
            return Assessment("high", f"Clicks “{target.strip()[:60]}”, which may buy, send, or change something.")
        if on_sensitive:
            return Assessment("high", "Clicks on a checkout/payment/account page.")
        return Assessment("low", "Clicks on a page.")
    if action == "type":
        if SENSITIVE_FIELD.search(target):
            return Assessment("high", "Types into a password or payment field.")
        return Assessment("medium" if on_sensitive else "low", "Types into the page.")
    if action == "press":
        return Assessment("high" if on_sensitive else "medium", "Presses a key, which may submit a form.")
    return Assessment("medium", f"Browser action '{action}'.")


# ------------------------------------------------------------------ policy
JUDGE_SYSTEM = ("You rate the risk of a shell command an AI assistant wants to run on the user's computer, "
                "inside a project workspace folder. Be careful but not paranoid.")
JUDGE_PROMPT = """Workspace: {workspace}
Command:
<command>
{command}
</command>

Rate it:
- safe: only reads or prints information
- low: small, easily undone changes inside the workspace
- medium: installs software, runs code, deletes workspace files, or downloads things
- high: affects things outside the workspace, sends data anywhere, needs admin rights, or is hard to undo
- critical: could destroy data or the system, or runs code fetched from the internet unseen
Give the level and a one-sentence reason a non-expert would understand."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"level": {"type": "string", "enum": LEVELS}, "reason": {"type": "string"}},
    "required": ["level", "reason"],
    "additionalProperties": False,
}


def rule_key(tool: str, args: dict) -> str:
    """What an 'always allow' rule matches on: the command's first two words, or the browser action."""
    if tool == "run_shell":
        toks = _tokens(_segments(args.get("command", "") or "")[0]) if args.get("command") else []
        return " ".join(toks[:2])
    return f"{args.get('action', '')}"


class Policy:
    def __init__(self, data_dir: Path, threshold: str = "low", unattended: str = "low"):
        self.path = Path(data_dir) / "safety.json"
        self.threshold = threshold if threshold in LEVELS[:-1] else "low"
        self.unattended = unattended if unattended in LEVELS[:-1] else "low"
        self.rules: list[dict] = []
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                self.threshold = saved.get("threshold", self.threshold)
                self.unattended = saved.get("unattended", self.unattended)
                self.rules = saved.get("rules", [])
            except ValueError:
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps({"threshold": self.threshold, "unattended": self.unattended,
                                         "rules": self.rules}, indent=2))

    def update(self, threshold: str | None = None, unattended: str | None = None) -> None:
        if threshold in LEVELS[:-1]:
            self.threshold = threshold
        if unattended in LEVELS[:-1]:
            self.unattended = unattended
        self.save()

    def matching_rule(self, tool: str, args: dict, level: str) -> dict | None:
        key = rule_key(tool, args)
        for r in self.rules:
            if r["tool"] == tool and r["key"] == key and rank(level) <= rank(r["max_level"]):
                return r
        return None

    def remember(self, tool: str, args: dict, level: str) -> dict | None:
        if level == "critical":
            return None
        key = rule_key(tool, args)
        if not key:
            return None
        rule = {"tool": tool, "key": key, "max_level": level, "created": time.time()}
        self.rules = [r for r in self.rules if not (r["tool"] == tool and r["key"] == key)] + [rule]
        self.save()
        return rule

    def forget(self, index: int) -> bool:
        if 0 <= index < len(self.rules):
            self.rules.pop(index)
            self.save()
            return True
        return False

    def decide(self, tool: str, args: dict, level: str, interactive: bool) -> tuple[str, str]:
        """-> (allow | ask | deny, why)"""
        if level == "critical":
            return ("ask", "critical actions always need you") if interactive else ("deny", "critical, nobody to ask")
        if rank(level) <= rank(self.threshold if interactive else self.unattended):
            return "allow", f"{level} is within your auto-approve level"
        rule = self.matching_rule(tool, args, level)
        if rule:
            return "allow", f"you always allow “{rule['key']}”"
        if not interactive:
            return "deny", f"{level} is above the unattended limit ({self.unattended})"
        return "ask", f"{level} is above your auto-approve level ({self.threshold})"
