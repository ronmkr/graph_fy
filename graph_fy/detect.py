# file discovery, type classification, and corpus health checks
from __future__ import annotations
import fnmatch
import json
import os
import re
import shlex
import stat
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Callable

from graph_fy.google_workspace import (
    GOOGLE_WORKSPACE_EXTENSIONS,
    convert_google_workspace_file,
    google_workspace_enabled,
)
from graph_fy.paths import graph_fy_OUT, out_path


class FileType(str, Enum):
    CODE = "code"
    DOCUMENT = "document"
    PAPER = "paper"
    IMAGE = "image"
    VIDEO = "video"


_MANIFEST_PATH = str(out_path("manifest.json"))

#: Window in which a manifest row's own timestamp is too close to the file's
#: mtime for "mtime unchanged" to prove the content is unchanged. Coarse for
#: filesystems that round mtime to whole seconds; tight when real sub-second
#: precision is reported. Mirrors cache.py's `_MTIME_GRANULARITY_NS` (2s) —
#: the same racily-clean assumption at the hash-cache layer (#2466 / #2612);
#: keep the two in sync.
_MTIME_COARSE_S = 2.0
_MTIME_SUBSECOND_S = 0.05

CODE_EXTENSIONS = {'.py', '.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.ejs', '.ets', '.go', '.rs', '.cbl', '.cob', '.cobol', '.cpy', '.java', '.groovy', '.gradle', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.cu', '.cuh', '.metal', '.rb', '.rake', '.swift', '.kt', '.kts', '.cs', '.scala', '.php', '.lua', '.luau', '.toc', '.zig', '.ps1', '.psm1', '.psd1', '.ex', '.exs', '.m', '.mm', '.ml', '.mli', '.jl', '.vue', '.svelte', '.astro', '.dart', '.v', '.sv', '.svh', '.sql', '.r', '.f', '.F', '.f90', '.F90', '.f95', '.F95', '.f03', '.F03', '.f08', '.F08', '.pas', '.pp', '.dpr', '.dpk', '.lpr', '.inc', '.dfm', '.lfm', '.lpk', '.sh', '.bash', '.json', '.yaml', '.yml', '.tf', '.tfvars', '.hcl', '.dm', '.dme', '.dmi', '.dmm', '.dmf', '.sln', '.slnx', '.csproj', '.fsproj', '.vbproj', '.xaml', '.razor', '.cshtml', '.cls', '.trigger', '.lisp', '.cl', '.lsp', '.asd', '.robot', '.resource', '.proto', '.avsc', '.bzl', '.bazel', '.cmake', '.ui'}
DOC_EXTENSIONS = {'.md', '.mdx', '.qmd', '.skill', '.txt', '.rst'}
PAPER_EXTENSIONS: set[str] = set()
IMAGE_EXTENSIONS: set[str] = set()
OFFICE_EXTENSIONS: set[str] = set()
VIDEO_EXTENSIONS: set[str] = set()
GOOGLE_WORKSPACE_EXTENSIONS: set[str] = set()

CORPUS_WARN_THRESHOLD = 50_000    # words - below this, warn "you may not need a graph"
CORPUS_UPPER_THRESHOLD = 500_000  # words - above this, warn about token cost
FILE_COUNT_UPPER = 500             # files - above this, warn about token cost

from graph_fy.office import (
    _OFFICE_MAX_RAW_BYTES,
    _OFFICE_MAX_DECOMPRESSED_BYTES,
    _OFFICE_MAX_COMPRESSION_RATIO,
    _file_within_size_cap,
    _zip_within_caps,
)


# Dedicated credential-store directories: everything beneath them is sensitive,
# with no carve-out — a .py inside ~/.ssh or ~/.aws is tooling for key material,
# not a source package, and keys there are routinely extensionless.
# Both sets are checked against path.parts[:-1] (parents only) so a root-level
# file named "credentials" or "secrets" is not falsely flagged by this stage.
_CREDENTIAL_STORE_DIRS = frozenset({
    ".ssh", ".gnupg", ".aws", ".gcloud",
})

# Bare-name directories that are as often legitimate source packages (Go
# internal/secrets, a credentials/ service module) as credential stores. Their
# contents are sensitive EXCEPT genuine programming-language source, mirroring
# the Stage 3 keyword carve-out (#1666) at the directory level (#1943).
_AMBIGUOUS_SENSITIVE_DIRS = frozenset({
    "secrets", ".secrets", "credentials",
})

# Files that may contain secrets - skip silently. These patterns are specific
# (extensions, exact credential-store names) and always apply.
_SENSITIVE_PATTERNS = [
    re.compile(r'(^|[\\/])\.(env|envrc)(\.|$)', re.IGNORECASE),
    re.compile(r'\.(pem|key|p12|pfx|cert|crt|der|p8)$', re.IGNORECASE),
    # SSH/GPG private keys. Left boundary + IGNORECASE so `grid_rsa` (alpha before
    # `id_rsa`) and `ID_RSA` are handled correctly, not matched as a substring.
    re.compile(r'(^|[^A-Za-z0-9])(id_rsa|id_dsa|id_ecdsa|id_ed25519)(\.pub)?$', re.IGNORECASE),
    re.compile(r'^secring(\.(gpg|pgp))?$', re.IGNORECASE),  # GPG private keyring
    # Auth/credential dotfiles that routinely hold tokens (#2106: .npmrc/.pypirc/
    # .git-credentials/.boto were silently indexed before).
    re.compile(r'(\.netrc|\.pgpass|\.htpasswd|\.npmrc|\.pypirc|\.git-credentials|\.boto)$', re.IGNORECASE),
    # NOTE: aws_credentials/gcloud_credentials/service_account moved to the
    # boundary-checked Stage 3 keyword path (#2106). The old unbounded
    # `service.account` substring (regex `.` wildcard) matched real source like
    # google/oauth2/service_account.py and prose like aws_credentials_rotation.md.
]

# Committed dotenv / envrc templates — placeholders only, not live secrets.
# Stage 2's `.env.` regex otherwise treats these like `.env.local` (#2184).
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _is_env_template(name: str) -> bool:
    """True for `.env.example` / `.envrc.sample` style committed templates (#2184)."""
    lower = name.lower()
    if not lower.endswith(_ENV_TEMPLATE_SUFFIXES):
        return False
    # Basename must still be an .env* / .envrc* file (not e.g. secrets.example).
    return bool(re.match(r"\.(env|envrc)\.", lower))

# Generic keyword patterns - these only count when the keyword is LOAD-BEARING
# in the filename (see _generic_keyword_hit), because a keyword buried mid-phrase
# in a long descriptive slug names a topic, not a credential store:
# "token-economics-of-recall.md" is a note ABOUT tokens; "api_token.txt" IS one.
# Uses lookarounds instead of \b so underscore-prefixed names like api_token.txt
# match. Both patterns use (?![a-zA-Z]) so that the trailing-underscore behavior
# is consistent: "secret_store.txt" IS flagged, "tokenizer.py" is NOT (because
# "i" after "token" is alpha and blocks the match).
# `token` is kept separate because its longer suffix "izer"/"ize" is the only
# common false-positive; other keywords have no such well-known derivatives.
_GENERIC_KEYWORD_PATTERNS = [
    re.compile(r'(?<![a-zA-Z0-9])(credential|secret|passwd|password|private_key)s?(?![a-zA-Z])', re.IGNORECASE),
    re.compile(r'(?<![a-zA-Z0-9])tokens?(?![a-zA-Z])', re.IGNORECASE),
    # service_account / service-account / serviceaccount (GCP key files). In the
    # keyword path so `service_account.py` (real source) is spared while
    # `service-account.json` (a downloaded key) and bare names are still caught
    # (#2106; was an unbounded Stage 2 substring). aws_credentials/gcloud_credentials
    # are already covered by the `credential` keyword above.
    re.compile(r'(?<![a-zA-Z0-9])service[._-]?account(?![a-zA-Z])', re.IGNORECASE),
]

# Prose/note formats: a heavily-linked wiki article whose topic slug ends in a
# keyword (privacy-tokens.md, token-economics.md) is a document ABOUT the topic,
# not a credential store, so it must not be silently dropped (#2106). A BARE
# keyword name (secrets.md, token.md, passwords.md) still reads as a dump and
# stays excluded — see _is_prose_note.
_PROSE_EXTS = frozenset({".md", ".markdown", ".rst", ".org", ".adoc", ".tex"})

# Bare PLURAL "tokens" in a prose file (TOKENS.md) reads as a design-token
# reference doc, not a credential dump — unlike every other generic keyword's
# plural (secrets.md, passwords.md, credentials.md still read as dumps) and
# unlike the singular "token.md", which stays excluded (#3527). Scoped to
# prose extensions only: "tokens.txt" is still a plausible secret store.
_BARE_PLURAL_TOKENS = re.compile(r'(?<![a-zA-Z0-9])tokens(?![a-zA-Z])', re.IGNORECASE)

# Data/serialization extensions that commonly ARE secret stores when their name
# hits a generic keyword (credentials.json, secrets.yaml, token.toml) or they sit
# in an ambiguous sensitive dir (secrets/db.json). These stay subject to the
# Stage 1 ambiguous-dir drop and the Stage 3 keyword drop even though some route
# through the CODE path for manifest parsing — only real programming-language
# source is exempt (#1666, #1943).
_SECRET_PRONE_DATA_EXTS = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config",
    ".xml", ".properties", ".env", ".txt",
    # .tfvars is Terraform's canonical VALUES store (routinely holds real
    # secrets), not source — keep it out of the graph even though it sits in
    # CODE_EXTENSIONS. .tf/.hcl are genuine infra source and stay graphable.
    ".tfvars",
})

# Word separators for the load-bearing check (underscore intentionally included;
# multi-word keywords like private_key are handled by the end-of-stem check,
# which runs before word counting).
_WORD_SPLIT = re.compile(r'[-_\s.]+')  # '.' included so `token.economics.notes` counts as 3 words (#2106)


def _is_prose_note(path: Path) -> bool:
    """A prose/note file (.md/.rst/...) whose stem is a multi-word topic slug is
    exempt from the generic-keyword drop (#2106). A stem that IS exactly a bare
    keyword (secrets / token / passwords) is NOT exempt — that still reads as a
    credential dump. The one exception is bare plural "tokens" (TOKENS.md),
    which reads as a design-token reference doc rather than a secret store
    (#3527)."""
    if path.suffix.lower() not in _PROSE_EXTS:
        return False
    stem = Path(path.name).stem.lstrip('.') or Path(path.name).stem
    if _BARE_PLURAL_TOKENS.fullmatch(stem):
        return True
    return not any(p.fullmatch(stem) for p in _GENERIC_KEYWORD_PATTERNS)


def _generic_keyword_hit(name: str) -> bool:
    """True if a generic secret keyword appears load-bearing in the filename.

    Secret-store files name their contents, and in English compounds the
    content noun is the head, which comes last: "github-personal-access-token",
    "api_token", "oauth_token". A keyword that is neither at the end of the
    stem nor in a short (<=2 word) name is a topic word in a descriptive slug
    ("token-economics-of-recall.md", "password-policy-discussion.md") and must
    not cause the file to be silently dropped from the graph (#436, #718).
    """
    # Stem = name minus only the FINAL extension (not up to the first dot), so a
    # multi-dot topic slug like `token.economics.notes.md` keeps all its words and
    # doesn't collapse to a bare `token` (#2106). Leading dots stripped so
    # dotfiles like `.token` keep their keyword.
    stem = Path(name).stem.lstrip('.') or Path(name).stem
    for pat in _GENERIC_KEYWORD_PATTERNS:
        hit = False
        for m in pat.finditer(stem):
            hit = True
            if m.end() == len(stem):  # keyword ends the stem -> names the contents
                return True
        if hit and len([w for w in _WORD_SPLIT.split(stem) if w]) <= 2:
            return True  # short name like token_config.yaml / secret_handler.txt
    return False


def _is_graphable_source(path: Path) -> bool:
    """True for genuine programming-language source — the only category exempt
    from the ambiguous-dir (Stage 1, #1943) and generic-keyword (Stage 3, #1666)
    drops. Data/serialization formats are NOT exempt even though some route
    through the CODE path for manifest parsing: credentials.json / secrets.yaml
    are exactly the stores those stages must keep catching.
    """
    return classify_file(path) == FileType.CODE and path.suffix.lower() not in _SECRET_PRONE_DATA_EXTS


def _is_sensitive(path: Path) -> bool:
    """Return True if this file likely contains secrets and should be skipped."""
    # Stage 1: any PARENT directory is a known secrets dir (parts[:-1] excludes
    # the filename itself so a root-level file named "credentials" is not falsely
    # skipped — the name patterns in Stage 2 handle the filename). Dedicated
    # credential stores drop everything unconditionally; ambiguous bare-name dirs
    # (secrets/, credentials/) spare genuine source (#1943), which still falls
    # through so Stages 2-3 screen its filename like anywhere else.
    parents = path.parts[:-1]
    # Lowercase the segment comparison so `Secrets/`/`SECRETS/` (real on
    # case-insensitive macOS/Windows filesystems) are still caught (#2106).
    if any(part.lower() in _CREDENTIAL_STORE_DIRS for part in parents):
        return True
    if any(part.lower() in _AMBIGUOUS_SENSITIVE_DIRS for part in parents) and not _is_graphable_source(path):
        return True
    # Stage 2: filename pattern match. Template suffixes (.example/.sample/…)
    # on .env / .envrc are the usual "safe to commit" convention — keep them
    # in the graph without opening a broad Stage 2 allowlist (#2184 / #1921).
    name = path.name
    if any(p.search(name) for p in _SENSITIVE_PATTERNS) and not _is_env_template(name):
        return True
    # Stage 3: generic keywords, only when load-bearing in the name. Do NOT let a
    # bare name keyword silently drop a genuine programming-language source file:
    # a .rb/.py named device_token or passwords_controller is a module, not a secret
    # store (#1666). Data/config formats (.json, .yaml, .toml, ...) are deliberately
    # NOT exempt even though .json routes through the CODE path for manifest parsing,
    # because credentials.json / oauth_token.json / secrets.yaml are exactly the
    # secret stores this stage must catch. The specific Stage 2 patterns (.env, .pem,
    # id_rsa, ...) still apply to everything regardless of extension.
    if _generic_keyword_hit(name):
        # Genuine source AND multi-word prose notes are exempt; a bare-keyword
        # name (secrets.md, token.txt) still drops (#1666, #2106).
        return not (_is_graphable_source(path) or _is_prose_note(path))
    return False



_ASSET_DIR_MARKERS = {".imageset", ".xcassets", ".appiconset", ".colorset", ".launchimage"}


_SHEBANG_CODE_INTERPRETERS = {
    "python", "python3", "python2",
    "ruby", "perl", "node", "nodejs",
    "bash", "sh", "dash", "zsh", "fish", "ksh", "tcsh",
    "lua", "php", "julia", "Rscript",
}


def _split_env_s(value: str, rest: list[str]) -> list[str]:
    """Re-tokenize an `env -S`/`--split-string` packed command, prepending the
    operand to any trailing args. Returns the unpacked argv."""
    packed = " ".join([value, *rest]).strip()
    return shlex.split(packed)


def _env_command_args(args: list[str], *, allow_split: bool = True) -> list[str]:
    """Strip leading env(1) options and var assignments, return the trailing
    command argv. Covers macOS/BSD and GNU coreutils env documented spellings.

    POSIX/macOS short forms:
        env [-0iv] [-C workdir] [-P utilpath] [-S string]
            [-u name] [name=value ...] [utility [argument ...]]

    GNU coreutils long/compact forms additionally supported:
        --argv0=ARG / -a ARG / -aARG
        --unset=NAME / --unset NAME / -u NAME / -uNAME
        --chdir=DIR / --chdir DIR / -C DIR / -CDIR
        --split-string=STRING / --split-string STRING
        -S STRING / -SSTRING / -vS STRING / -vSSTRING
        --ignore-environment / --null / --debug / --list-signal-handling
        --default-signal[=SIG] / --ignore-signal[=SIG] / --block-signal[=SIG]

    `-S` / `--split-string` payloads are themselves env-style argument lists
    per the GNU shebang synopsis:
        #!/usr/bin/env -[v]S[option]... [name=value]... command [args]...
    so after splitting the payload we recursively re-parse it with
    `allow_split=False` (a nested -S inside a split payload is rejected to
    bound recursion).

    Unknown hyphen-prefixed args yield [] (we refuse to guess whether
    their next token is an interpreter or an operand).
    """
    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--":
            return args[i + 1:]

        # Split-string forms: tokenize the packed payload, then re-parse it
        # as env args (so leading assignments/flags inside the payload are
        # skipped before the interpreter is identified).
        if allow_split:
            if arg == "-S":
                if i + 1 >= len(args):
                    return []
                return _env_command_args(
                    _split_env_s(" ".join(args[i + 1:]), []),
                    allow_split=False,
                )
            if arg.startswith("-S") and len(arg) > 2:
                return _env_command_args(
                    _split_env_s(arg[2:], args[i + 1:]),
                    allow_split=False,
                )
            if arg == "-vS":
                if i + 1 >= len(args):
                    return []
                return _env_command_args(
                    _split_env_s(" ".join(args[i + 1:]), []),
                    allow_split=False,
                )
            if arg.startswith("-vS") and len(arg) > 3:
                return _env_command_args(
                    _split_env_s(arg[3:], args[i + 1:]),
                    allow_split=False,
                )
            if arg.startswith("--split-string="):
                return _env_command_args(
                    _split_env_s(arg.split("=", 1)[1], args[i + 1:]),
                    allow_split=False,
                )
            if arg == "--split-string":
                if i + 1 >= len(args):
                    return []
                return _env_command_args(
                    _split_env_s(args[i + 1], args[i + 2:]),
                    allow_split=False,
                )

        # Options with separate required operand
        if arg in {"-u", "-C", "-P", "-a", "--unset", "--chdir", "--argv0"}:
            if i + 2 > len(args):
                return []
            i += 2
            continue

        # Clumped short option + operand
        if (
            arg.startswith(("-u", "-C", "-P", "-a"))
            and len(arg) > 2
            and not arg.startswith("--")
        ):
            i += 1
            continue

        # Long option with `=` operand
        if arg.startswith(("--unset=", "--chdir=", "--argv0=")):
            i += 1
            continue

        # No-operand flags
        if arg in {"-", "-i", "-0", "-v", "--ignore-environment", "--null",
                   "--debug", "--list-signal-handling"}:
            i += 1
            continue

        # Signal-handling long flags (with or without =SIG operand — we treat
        # them as no-effect for interpreter-resolution purposes)
        if arg.startswith(("--default-signal", "--ignore-signal", "--block-signal")):
            i += 1
            continue

        # Unknown hyphen-prefixed: refuse to guess
        if arg.startswith("-"):
            return []

        # Inline NAME=value assignment
        if "=" in arg:
            i += 1
            continue

        # First non-option, non-assignment token starts the command argv
        return args[i:]

    return []


def _shebang_interpreter(path: Path) -> str | None:
    """Return the interpreter name from a shebang line.

    Handles forms that a naive parser misses:
      - `#!/usr/bin/env -S python3 -u`     (env -S split-args form, anywhere)
      - `#!/usr/bin/env -i bash`           (no-operand env flags)
      - `#!/usr/bin/env -u VAR python3`    (env options with operands)
      - `#!/usr/bin/env -C /tmp python3`   (env -C workdir)
      - `#!/usr/bin/env -P /bin python3`   (env -P utilpath)
      - `#!/usr/bin/env DEBUG=1 python3`   (inline var assignment)
      - `#!"/usr/local/bin/python with spaces"`  (shlex handles quotes)

    Returns the basename of the resolved interpreter, or None if there is
    no shebang / the file is unreadable / parsing fails.
    """
    try:
        with path.open("rb") as f:
            first = f.read(256)
        if not first.startswith(b"#!"):
            return None
        line = first.split(b"\n")[0].decode(errors="replace")[2:].strip()
        parts = shlex.split(line)
        if not parts:
            return None
        interp = Path(parts[0]).name
        if interp == "env":
            env_args = _env_command_args(parts[1:])
            if not env_args:
                return None
            interp = Path(env_args[0]).name
        return interp
    except (OSError, ValueError):
        return None


def _shebang_file_type(path: Path) -> FileType | None:
    """Peek at the first line of an extensionless file for a shebang."""
    interp = _shebang_interpreter(path)
    if interp in _SHEBANG_CODE_INTERPRETERS:
        return FileType.CODE
    return None


def classify_file(path: Path) -> FileType | None:
    # Package manifests (apm.yml, pyproject.toml, Cargo.toml, go.mod, pom.xml) are parsed
    # deterministically, so route them to the AST path (CODE) rather than the LLM
    # document path — otherwise apm.yml (a .yml "document") would be LLM-extracted
    # and a package would split into duplicate file-anchored nodes (#1377).
    from graph_fy.manifest_ingest import is_package_manifest_path
    if is_package_manifest_path(path):
        return FileType.CODE
    # Compound extensions must be checked before simple suffix lookup
    if path.name.lower().endswith(".blade.php"):
        return FileType.CODE
    # Infrastructure-as-code and config formats (Kubernetes, Helm, CloudFormation, Kafka)
    try:
        from graph_fy.extractors.iac import (
            is_kubernetes_manifest,
            is_helm_chart_file,
            is_cloudformation_template,
            is_kafka_config,
        )
        if (
            is_helm_chart_file(path)
            or is_kubernetes_manifest(path)
            or is_cloudformation_template(path)
            or is_kafka_config(path)
        ):
            return FileType.CODE
    except Exception:
        pass
    # Dockerfiles and Docker Compose
    from graph_fy.extractors.docker import is_docker_compose
    if path.name.lower() == "dockerfile" or path.name.lower().startswith("dockerfile.") or path.suffix.lower() == ".dockerfile" or is_docker_compose(path):
        return FileType.CODE
    # GitHub Actions workflows
    from graph_fy.extractors.github_actions import is_github_workflow
    if is_github_workflow(path):
        return FileType.CODE
    # OpenAPI, AsyncAPI, and JSON Schema
    from graph_fy.extractors.api_schema import is_api_or_schema
    if is_api_or_schema(path):
        return FileType.CODE
    # Argo CD GitOps manifests
    from graph_fy.extractors.argocd import is_argocd_manifest
    if is_argocd_manifest(path):
        return FileType.CODE
    # Bazel and Monorepo configuration
    from graph_fy.extractors.bazel import is_bazel_file
    from graph_fy.extractors.monorepo import is_monorepo_file
    if is_bazel_file(path) or is_monorepo_file(path):
        return FileType.CODE
    # CMake build configuration
    from graph_fy.extractors.cmake import is_cmake_file
    if is_cmake_file(path):
        return FileType.CODE
    # Qt Designer UI
    from graph_fy.extractors.qt_ui import is_qt_ui_file
    if is_qt_ui_file(path):
        return FileType.CODE
    # Gradle build configuration
    from graph_fy.extractors.gradle import is_gradle_file
    if is_gradle_file(path):
        return FileType.CODE
    if path.name.endswith(".component.html"):
        return FileType.CODE

    ext = path.suffix.lower()
    if not ext:
        return _shebang_file_type(path)
    if ext in CODE_EXTENSIONS:
        return FileType.CODE
    if ext in DOC_EXTENSIONS:
        return FileType.DOCUMENT
    return None


from graph_fy.office import (
    pdf_to_markdown,
    extract_pdf_text,
    docx_to_markdown,
    xlsx_to_markdown,
    xlsx_extract_structure,
    convert_office_file,
    count_words,
)



from graph_fy.ignore import (
    _is_regular_file,
    _SKIP_DIRS,
    _SKIP_FILES,
    _JS_SNAPSHOT_TEST_ROOTS,
    _COVERAGE_ARTIFACT_FILES,
    _COVERAGE_ARTIFACT_DIRS,
    _has_coverage_artifacts,
    _BUILD_OUTPUT_SUFFIXES,
    _has_build_output_markers,
    _has_venv_markers,
    _is_noise_dir,
    _VCS_MARKERS,
    _nfc,
    _parse_gitignore_line,
    _is_valid_git_marker,
    _has_vcs_marker,
    _find_vcs_root,
    _path_identity,
    _git_tracked_path_keys,
    _git_info_exclude,
    _warned_ignore_encodings,
    _read_ignore_text,
    _load_dir_own_ignore,
    _load_graph_fyignore,
    _PARSED_PATTERN_CACHE,
    _PARSED_PATTERN_CACHE_MAX,
    _parse_ignore_pattern,
    _CASEFOLD_PATHS,
    _lexical_relative,
    _match_globstar_parts,
    _match_anchored_ignore_pattern,
    _is_ignored,
    _is_scan_ignored,
    ignored_predicate,
)




def _resolves_under_root(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to a target inside ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _is_redundant_git_worktree(dir_path: Path, root: Path) -> bool:
    """Return True if dir_path is a linked git worktree whose main checkout is under root."""
    try:
        git_file = dir_path / ".git"
        if not git_file.is_file():
            return False
        content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if not content.startswith("gitdir:"):
        return False
    gitdir_str = content[7:].strip()
    gitdir_path = (dir_path / gitdir_str).resolve()
    # Check commondir pointer inside worktree gitdir
    common_dir_file = gitdir_path / "commondir"
    if common_dir_file.is_file():
        try:
            commondir_str = common_dir_file.read_text(encoding="utf-8", errors="replace").strip()
            main_dot_git = (gitdir_path / commondir_str).resolve()
            main_checkout = main_dot_git.parent
            if _resolves_under_root(main_checkout, root):
                return True
        except OSError:
            pass
    parts = gitdir_path.parts
    if "worktrees" in parts:
        wt_idx = parts.index("worktrees")
        if wt_idx >= 1:
            main_dot_git = Path(*parts[:wt_idx])
            main_checkout = main_dot_git.parent if main_dot_git.name == ".git" else main_dot_git
            if _resolves_under_root(main_checkout, root):
                return True
    return False


def detect(root: Path, *, follow_symlinks: bool | None = None, google_workspace: bool | None = None, extra_excludes: list[str] | None = None, cache_root: Path | None = None, gitignore: bool = True) -> dict:
    root = root.resolve()
    configured_out_dir = root / graph_fy_OUT
    configured_out_names = {configured_out_dir.name}
    try:
        configured_out_dir = configured_out_dir.resolve()
    except (OSError, RuntimeError):
        configured_out_dir = configured_out_dir.absolute()
    configured_out_names.add(configured_out_dir.name)
    # .graph_fyinclude support was removed (#2112): its loader and matchers had
    # no consumers, so the file has been a silent no-op since dot directories
    # became indexed by default (#873). Surface that once per scan so a
    # leftover allowlist file is not a silent behavior change.
    if (root / ".graph_fyinclude").is_file():
        import sys as _sys
        print(
            "[graph_fy] WARNING: .graph_fyinclude is no longer supported "
            "(it has been non-functional since dot directories became indexed "
            "by default); to re-include ignored paths, use ! negation patterns "
            "in .graph_fyignore.",
            file=_sys.stderr,
        )
    if follow_symlinks is None:
        follow_symlinks = False
    google_workspace = google_workspace_enabled() if google_workspace is None else google_workspace
    files: dict[FileType, list[str]] = {
        FileType.CODE: [],
        FileType.DOCUMENT: [],
        FileType.PAPER: [],
        FileType.IMAGE: [],
        FileType.VIDEO: [],
    }
    total_words = 0

    def _wc(path: Path) -> int:
        # Cache word counts against each file's stat signature so unchanged
        # PDFs/docx aren't re-parsed on every run just to size the corpus (#1656).
        # cache_root (when given, e.g. from `extract --out`) keeps this cache out
        # of the scanned corpus (#1747).
        from graph_fy import cache as _cache
        return _cache.cached_word_count(path, root, count_words, cache_root=cache_root)

    skipped_sensitive: list[str] = []
    unclassified: list[str] = []
    # Files/dirs dropped by a .gitignore/.graph_fyignore rule. Recorded so an
    # over-broad ignore (or a legitimately-ignored subtree) is visible instead
    # of silently vanishing from the graph (#1922). Directory-level entries keep
    # this bounded — a pruned `data/` is one entry, not one per contained file.
    ignored: list[str] = []
    pruned_noise: list[str] = []
    ignore_patterns = _load_graph_fyignore(root, gitignore=gitignore)
    explicit_ignore_patterns = _load_graph_fyignore(root, gitignore=False)
    # See ignored_predicate: skip the `git ls-files` subprocess when .gitignore
    # contributes no patterns, so a non-.gitignore corpus pays nothing for it.
    tracked_files, tracked_dirs = (
        _git_tracked_path_keys(root)
        if gitignore and len(ignore_patterns) > len(explicit_ignore_patterns)
        else (set(), set())
    )
    ignore_cache: dict[Path, bool] = {}  # shared across all _is_ignored calls in this scan
    explicit_ignore_cache: dict[Path, bool] = {}
    # CLI --exclude patterns are anchored at the scan root and appended last
    # so they win over any .graph_fyignore/.gitignore rules (#947).
    if extra_excludes:
        for pat in extra_excludes:
            line = _parse_gitignore_line(pat)
            if line:
                ignore_patterns.append((root, line))
                explicit_ignore_patterns.append((root, line))

    def _ignored_for_scan(path: Path) -> bool:
        return _is_scan_ignored(
            path,
            root,
            ignore_patterns,
            explicit_ignore_patterns,
            tracked_files,
            tracked_dirs,
            cache=ignore_cache,
            explicit_cache=explicit_ignore_cache,
        )

    # Always include graph_fy_out/memory/ - query results filed back into the graph
    memory_dir = root / graph_fy_OUT / "memory"
    scan_paths = [root]
    if memory_dir.exists():
        scan_paths.append(memory_dir)

    seen: set[Path] = set()
    all_files: list[Path] = []

    # os.walk swallows os.scandir errors by default (no onerror -> the failing
    # directory subtree is silently skipped). That turns a transient
    # PermissionError, or a directory created/deleted mid-walk (e.g. concurrent
    # writes racing the scan), into a partial file list and, downstream, a
    # silently partial graph.json. Record and surface every skipped directory
    # so an incomplete enumeration is visible rather than silent.
    walk_errors: list[str] = []

    def _on_walk_error(err: OSError) -> None:
        import sys as _sys
        target = getattr(err, "filename", None) or "<unknown>"
        walk_errors.append(f"{target}: {err}")
        print(
            f"[graph_fy] WARNING: could not scan {target} ({err}); "
            f"its files are missing from this run's enumeration.",
            file=_sys.stderr,
        )

    for scan_root in scan_paths:
        in_memory_tree = memory_dir.exists() and str(scan_root).startswith(str(memory_dir))
        for dirpath, dirnames, filenames in os.walk(
            scan_root, followlinks=follow_symlinks, onerror=_on_walk_error
        ):
            dp = Path(dirpath)
            if follow_symlinks and os.path.islink(dirpath):
                real = os.path.realpath(dirpath)
                parent_real = os.path.realpath(os.path.dirname(dirpath))
                if parent_real == real or parent_real.startswith(real + os.sep):
                    dirnames.clear()
                    continue
            if not in_memory_tree:
                # dp == root was already loaded by _load_graph_fyignore (root is
                # the last entry in its ancestor chain); every other directory
                # reached by the walk is a descendant below the scan root, whose
                # own .gitignore/.graph_fyignore is unknown until we get here.
                # Load it now, before pruning dp's children, so a nested ignore
                # file governs its own subtree the same way git honors it (#1206).
                if dp != root:
                    ignore_patterns.extend(_load_dir_own_ignore(dp, gitignore=gitignore))
                    explicit_ignore_patterns.extend(
                        _load_dir_own_ignore(dp, gitignore=False)
                    )
                # Prune noise dirs in-place so os.walk never descends into them.
                # Dot dirs are allowed — users often want .github/, .claude/, etc.
                # Framework caches (.next, .nuxt, …) are caught by _is_noise_dir.
                # Negations need no special-casing here: _is_ignored already applies
                # last-match-wins (so `!dir/` un-ignores a directory and it won't be
                # pruned) and the gitignore parent-exclusion rule (a `!` cannot rescue
                # a file beneath an excluded dir), so descending an ignored directory to
                # look for a re-included file is never necessary. The previous blanket
                # `has_negation` disabled directory pruning for EVERY ignored dir whenever
                # any `!` rule existed — e.g. a single `!docs/**` made the walk descend
                # bin/, obj/, wwwroot/, generated/, … : a pathological slowdown on large
                # repos for no correctness gain.
                kept_dirs: list[str] = []
                for d in dirnames:
                    child = dp / d
                    is_configured_out = False
                    if d in configured_out_names:
                        try:
                            is_configured_out = child.resolve() == configured_out_dir
                        except (OSError, RuntimeError):
                            pass
                    if is_configured_out:
                        pruned_noise.append(str(child) + os.sep)
                        continue
                    if _is_redundant_git_worktree(child, root):
                        pruned_noise.append(str(child) + os.sep)
                        continue
                    if _is_noise_dir(d, dp):
                        # Record pruned-as-noise dirs so a wrongly-pruned real
                        # source dir is at least traceable in the output rather
                        # than vanishing silently (#2058).
                        pruned_noise.append(str(child) + os.sep)
                        continue
                    # Directory-level pruning: ONE ignore evaluation excludes the
                    # whole subtree — os.walk never descends, so a 29k-file
                    # ignored dir costs one check, not one per contained file.
                    if _ignored_for_scan(child):
                        ignored.append(str(child) + os.sep)
                        continue
                    kept_dirs.append(d)
                dirnames[:] = kept_dirs
                if follow_symlinks:
                    safe_dirs: list[str] = []
                    for d in dirnames:
                        child = dp / d
                        if child.is_symlink() and not _resolves_under_root(child, root):
                            skipped_sensitive.append(str(child) + " [symlink target outside scan root]")
                            continue
                        safe_dirs.append(d)
                    dirnames[:] = safe_dirs
            for fname in filenames:
                if fname in _SKIP_FILES:
                    continue
                p = dp / fname
                if p not in seen:
                    seen.add(p)
                    all_files.append(p)

    all_files.sort(key=lambda p: str(p))

    out_base = Path(cache_root).resolve() if cache_root is not None else root
    converted_dir = out_base / graph_fy_OUT / "converted"

    for p in all_files:
        # For memory dir files, skip hidden/noise filtering
        in_memory = memory_dir.exists() and str(p).startswith(str(memory_dir))
        if not in_memory:
            # Skip files inside our own converted/ dir (avoid re-processing sidecars)
            if str(p).startswith(str(converted_dir)):
                continue
        if not in_memory and _ignored_for_scan(p):
            ignored.append(str(p))
            continue
        if not _resolves_under_root(p, root):
            skipped_sensitive.append(str(p) + " [symlink target outside scan root]")
            continue
        if not _is_regular_file(p):
            # A repository may contain named pipes, sockets and device nodes,
            # and `clone <github-url>` exists precisely to point the scan at
            # trees the operator did not write.
            #
            # This has to be caught HERE, at the one place where a path is
            # admitted to the corpus, rather than at each read: the readers
            # number in the hundreds across the extractors, and open() on a
            # FIFO with no writer BLOCKS FOREVER — it never raises, so their
            # try/except cannot help and the whole run hangs with no output.
            skipped_sensitive.append(str(p) + " [not a regular file]")
            continue
        if _is_sensitive(p):
            skipped_sensitive.append(str(p))
            continue
        ftype = classify_file(p)
        if not ftype:
            # Considered but unclassifiable: an extension not in any supported set,
            # or an extensionless, non-shebang file (Dockerfile, Gemfile, Makefile,
            # Rakefile, LICENSE, ...). Previously these left no trace at all — not
            # counted, not listed — so a user couldn't tell they were seen (#1692).
            unclassified.append(str(p))
            continue
        if ftype:
            if p.suffix.lower() in GOOGLE_WORKSPACE_EXTENSIONS:
                if not google_workspace:
                    skipped_sensitive.append(
                        str(p)
                        + " [Google Workspace shortcut skipped - pass --google-workspace "
                        "or set graph_fy_GOOGLE_WORKSPACE=1]"
                    )
                    continue
                try:
                    md_path = convert_google_workspace_file(p, converted_dir, xlsx_to_markdown=xlsx_to_markdown, root=root)
                except Exception as exc:
                    skipped_sensitive.append(str(p) + f" [Google Workspace export failed: {exc}]")
                    continue
                if md_path:
                    # #3504: the sidecar lands under converted_dir, which the
                    # documented .gitignore advice puts inside a gitignored
                    # graph_fy_out/ -- an ignore check here would reject the
                    # tool's own output for the same reason it should be
                    # gitignored in the first place, silently dropping the
                    # source document from the corpus. The ignore check exists
                    # to keep USER files out of the scan, not to filter output
                    # this same pass just produced from an already-admitted file.
                    if _ignored_for_scan(md_path) and not md_path.is_relative_to(converted_dir):
                        continue
                    files[ftype].append(str(md_path))
                    total_words += _wc(md_path)
                else:
                    skipped_sensitive.append(str(p) + " [Google Workspace export produced no readable text]")
                continue
            # Office files: convert to markdown sidecar so subagents can read them
            if p.suffix.lower() in OFFICE_EXTENSIONS:
                md_path = convert_office_file(p, converted_dir, root=root)
                if md_path:
                    # #3504: see the matching comment in the Google Workspace
                    # branch above -- same sidecar-under-a-gitignored-output-dir
                    # trap, same exemption.
                    if _ignored_for_scan(md_path) and not md_path.is_relative_to(converted_dir):
                        continue
                    files[ftype].append(str(md_path))
                    total_words += _wc(md_path)
                else:
                    # Conversion failed (library not installed) - skip with note
                    skipped_sensitive.append(str(p) + " [office conversion failed - pip install graph_fyy[office]]")
                continue
            files[ftype].append(str(p))
            if ftype != FileType.VIDEO:
                total_words += _wc(p)

    for ftype in files:
        files[ftype].sort()

    total_files = sum(len(v) for v in files.values())
    needs_graph = total_words >= CORPUS_WARN_THRESHOLD

    # Determine warning - lower bound, upper bound, or sensitive files skipped
    warning: str | None = None
    if not needs_graph:
        warning = (
            f"Corpus is ~{total_words:,} words - fits in a single context window. "
            f"You may not need a graph."
        )
    elif total_words >= CORPUS_UPPER_THRESHOLD or total_files >= FILE_COUNT_UPPER:
        warning = (
            f"Large corpus: {total_files} files · ~{total_words:,} words. "
            f"Semantic extraction will be expensive (many Claude tokens). "
            f"Consider running on a subfolder."
        )

    return {
        "files": {k.value: v for k, v in files.items()},
        "total_files": total_files,
        "total_words": total_words,
        "needs_graph": needs_graph,
        "warning": warning,
        "skipped_sensitive": skipped_sensitive,
        "unclassified": sorted(unclassified),
        "walk_errors": walk_errors,
        "ignored": sorted(ignored),
        "pruned_noise_dirs": sorted(pruned_noise),
        "graph_fyignore_patterns": len(ignore_patterns),
        "scan_root": str(root.resolve()),
    }


from graph_fy.paths import os_path as _os_path


def __getattr__(name: str):
    """Lazily re-export manifest symbols to avoid cyclic import with manifest.py."""
    if name in {
        "_md5_file",
        "_stat_and_hash",
        "_to_relative_for_storage",
        "_to_absolute_from_storage",
        "load_manifest",
        "save_manifest",
        "_mtime_may_hide_a_rewrite",
        "detect_incremental",
    }:
        import graph_fy.manifest as _manifest
        return getattr(_manifest, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


