"""LOG CALLS CARRY STATIC TEMPLATES. Enforced by AST, not grep.

Spec §2. An interpolated log message captures whatever is in scope, which is
how a credential reaches a log line without anyone choosing to put it there.
This project's two real leaks were both an exception's own text: a presigned
URL inside a url.Error, a hostname inside a DNSError -- each one `str(exc)`
away from an innocent-looking log call.

GREP CANNOT DO THIS. It misses calls split across lines -- which is how most of
this codebase's log sites are written -- and it flags matching text inside
comments and docstrings. The AST distinguishes all of that.

MEASURED BEFORE WRITING: all existing sites already comply -- zero f-strings,
zero .format() on a message, zero str(exc) in a log call. This test therefore
LOCKS IN practice the codebase already follows. It should be green on its first
run; if it is not, that is a finding rather than a migration.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def _is_log_call(node: ast.Call) -> bool:
    """`<something>.debug(...)` etc. Matched on the ATTRIBUTE name rather than
    the receiver, so `logger`, `log`, `self._log` and `logging.getLogger(x)`
    are all covered without a naming convention."""
    return isinstance(node.func, ast.Attribute) and node.func.attr in _LEVELS


def _is_exception_annotation(node: ast.expr) -> bool:
    """Does this annotation name an exception type?

    I-4: an `ast.Name` (`BaseException`, `ValueError`, a project-defined
    exception) or an `ast.Attribute` (`somemod.SomeError`) whose identifier
    ends in `Error` or `Exception` -- the latter alone covers `BaseException`
    itself. Anything else (a subscript, a `|` union, a string forward-ref) is
    not matched; the four real sites this rule exists for all use a plain
    name or dotted name, and a missed exotic annotation only means a %-arg
    goes unflagged, not that a safe one is flagged."""
    if isinstance(node, ast.Name):
        ident = node.id
    elif isinstance(node, ast.Attribute):
        ident = node.attr
    else:
        return False
    return ident.endswith("Error") or ident.endswith("Exception")


def _bound_exception_names(scope_root: ast.AST) -> frozenset[str]:
    """Every name the AST's own binding information says holds an exception
    object, DIRECTLY within `scope_root`'s own scope: an `except ... as X`
    handler's name, and a function parameter annotated with an exception
    type. This is deliberately NOT a name heuristic (flagging every
    `exc`/`err` identifier) -- it is read off the binding itself, so a
    variable that happens to be named `exc` for an unrelated value is not
    flagged, and a differently-named exception (`app/worker.py`'s
    `contain(self, org_id: UUID, exc: BaseException)`, where `exc` is a
    PARAMETER rather than an `except`-clause name) still is.

    M-11: `scope_root` is ONE function (or the module, for top-level code
    outside any function) -- this does NOT descend into a NESTED
    function/async function's body, which is its own scope with its own call
    to this same helper. Before this fix, the caller collected names for the
    whole MODULE once, so `except ValueError as item:` in one function made
    `item` used as a %-arg in ANY OTHER function in the same file look like
    the same leak -- a verified false positive: Python itself deletes an
    `except ... as X` binding when its handler exits, so the name is not even
    valid for the rest of ITS OWN function, let alone the rest of the file."""
    names: set[str] = set()

    def collect(node: ast.AST, *, is_scope_root: bool) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not is_scope_root:
            # A nested def is its own scope -- its bindings are collected
            # separately, when the outer walk in `_violations_in_tree`
            # reaches it, not folded into this one.
            return
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None and _is_exception_annotation(arg.annotation):
                    names.add(arg.arg)
        for child in ast.iter_child_nodes(node):
            collect(child, is_scope_root=False)

    collect(scope_root, is_scope_root=True)
    return frozenset(names)


def _extra_dict_values(node: ast.Call) -> list[ast.expr]:
    """The values inside an `extra={...}` dict literal passed as a keyword
    argument to a log call, or `[]` when there is no such keyword or its
    value is not a dict literal -- a non-literal `extra=` (a variable, a
    function call building the dict) has nothing an AST sweep can see inside
    it, so it is silently skipped rather than flagged."""
    values: list[ast.expr] = []
    for kw in node.keywords:
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            values.extend(value for value in kw.value.values if value is not None)
    return values


def _call_violations(node: ast.Call, where: str, exception_names: frozenset[str]) -> list[str]:
    """Everything wrong with one log call, given the exception names bound in
    its own enclosing scope.

    I-4: this used to check only `node.args` (%-args) -- `node.keywords` was
    never walked, so `extra={"reason": str(exc)}` and `extra={"r": f"{exc}"}`
    sailed through: a `str` under a non-secret field name that passes
    `_safe_value` untouched and reaches the line verbatim under every
    formatter, and exactly what a developer reaches for immediately after
    being told "put values in extra=, never a %-arg". The same checks now run
    against an `extra=` dict literal's values, and `repr(...)` is flagged
    alongside `str(...)` everywhere -- `repr(exc)` renders `exc.args`, the
    same leak shape through the other stdlib stringifier."""
    found: list[str] = []
    if node.args:
        message = node.args[0]
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            found.append(f"{where}: message is not a string literal")
    for index, arg in enumerate(node.args):
        if isinstance(arg, ast.JoinedStr):
            found.append(f"{where}: f-string in a log call")
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id in (
            "str",
            "repr",
        ):
            found.append(f"{where}: {arg.func.id}(...) in a log call")
        # I-4: only args[1:] can be a %-arg -- args[0] is the template --
        # so the template itself is exempt even though its name can never
        # collide here (a message is always a string literal, never an
        # `ast.Name`).
        if index > 0 and isinstance(arg, ast.Name) and arg.id in exception_names:
            found.append(
                f"{where}: exception object (bound by except/annotation) passed "
                "as a %-arg -- renders byte-identical to str(exc); pass "
                "exc_info=<name> instead"
            )
    for value in _extra_dict_values(node):
        if isinstance(value, ast.JoinedStr):
            found.append(f"{where}: f-string in an extra= value")
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in (
            "str",
            "repr",
        ):
            found.append(f"{where}: {value.func.id}(...) in an extra= value")
    return found


def _violations_in_tree(tree: ast.AST, where_prefix: str) -> list[str]:
    """The checks for one already-parsed module, split out of `_violations`
    so a test can drive it against an inline AST fixture (a source string
    parsed with `ast.parse`, not a file on disk) instead of needing a
    throwaway file under `app/` --
    `test_the_gate_flags_an_exception_bound_name_used_as_a_percent_arg` is
    what that buys: proof the RULE fires, not just that today's four sites
    happen to be clean.

    M-11: exception names are resolved PER ENCLOSING FUNCTION -- or the
    module's own top level, for a log call outside any function -- rather
    than once for the whole tree; see `_bound_exception_names`."""
    found: list[str] = []

    def walk(node: ast.AST, exception_names: frozenset[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            exception_names = _bound_exception_names(node)
        if isinstance(node, ast.Call) and _is_log_call(node):
            where = f"{where_prefix}:{node.lineno}"
            found.extend(_call_violations(node, where, exception_names))
        for child in ast.iter_child_nodes(node):
            walk(child, exception_names)

    walk(tree, _bound_exception_names(tree))
    return found


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found.extend(_violations_in_tree(tree, str(path.relative_to(APP.parent))))
    return found


def test_every_log_call_uses_a_static_template() -> None:
    problems = _violations()
    assert not problems, (
        "log calls must carry a constant message and put values in extra=; "
        "exceptions are logged by type, never str(exc):\n  " + "\n  ".join(problems)
    )


def test_the_gate_flags_an_exception_bound_name_used_as_a_percent_arg() -> None:
    """I-4: `_violations()` used to only flag an explicit `str(exc)` call, so
    the byte-identical leak of passing the exception OBJECT as a %-arg
    (`logger.error("...: %s", exc)`, which renders `str(exc)` exactly the
    same way) sailed through undetected -- that hole is how four shipped call
    sites leaked a DSN and a hostname. Driven off an inline AST fixture (a
    source string, not a file on disk) so this pins the RULE rather than any
    one fix site -- without it, a future refactor could quietly neuter the
    check and the suite would stay green, which is the failure mode this file
    exists to prevent."""
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        logger.error('boom: %s', exc)\n"
    )
    tree = ast.parse(source, filename="<fixture>")
    found = _violations_in_tree(tree, "<fixture>")
    assert found, "an except-bound exception name passed as a %-arg must be flagged"


def test_extra_with_str_of_a_bound_exception_is_flagged() -> None:
    """I-4: `extra={"reason": str(exc)}` is exactly what a developer reaches
    for right after being told "put values in extra=, never a %-arg" -- and
    before this fix it sailed through untouched, because the sweep only ever
    walked `node.args`."""
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        logger.error('boom', extra={'reason': str(exc)})\n"
    )
    tree = ast.parse(source, filename="<fixture>")
    found = _violations_in_tree(tree, "<fixture>")
    assert found, "str(exc) inside an extra= dict literal must be flagged"


def test_extra_with_an_fstring_of_a_bound_exception_is_flagged() -> None:
    """I-4: the f-string twin of the case above -- `extra={"r": f"{exc}"}`
    interpolates the exception's text exactly as an f-string message would,
    just one level down inside the dict literal."""
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        logger.error('boom', extra={'r': f'{exc}'})\n"
    )
    tree = ast.parse(source, filename="<fixture>")
    found = _violations_in_tree(tree, "<fixture>")
    assert found, "an f-string inside an extra= dict literal must be flagged"


def test_a_percent_arg_repr_of_a_bound_exception_is_flagged() -> None:
    """I-4: `repr(exc)` renders `exc.args` into the message text -- the same
    leak shape as `str(exc)`, reached through the OTHER stdlib stringifier,
    which the sweep did not check for at all before this fix."""
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as exc:\n"
        "        logger.error('boom: %s', repr(exc))\n"
    )
    tree = ast.parse(source, filename="<fixture>")
    found = _violations_in_tree(tree, "<fixture>")
    assert found, "repr(exc) as a %-arg must be flagged"


def test_an_exception_bound_name_in_one_function_does_not_poison_another() -> None:
    """M-11: `_bound_exception_names` (formerly `_exception_bound_names`) used
    to be computed ONCE for the whole MODULE, so `except ... as item:` in one
    function made an unrelated parameter named `item` in a DIFFERENT function
    look like the same exception-as-%-arg leak -- a verified false positive,
    not a hypothetical one: Python deletes an `except ... as X` binding when
    its own handler exits, so the name is not even valid for the rest of its
    OWN function, let alone the rest of the file."""
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as item:\n"
        "        pass\n"
        "\n"
        "def g(item):\n"
        "    logger.warning('queued %s', item)\n"
    )
    tree = ast.parse(source, filename="<fixture>")
    found = _violations_in_tree(tree, "<fixture>")
    assert not found, (
        "an except-bound name in one function must not poison an unrelated "
        f"parameter of the same name in a different function; got: {found}"
    )


def test_the_sweep_actually_finds_log_calls() -> None:
    """Without this, a broken matcher reports a perfectly disciplined codebase.

    Measured 2026-08-14: 61 call sites (see spec §2.2 for how the earlier,
    grep-based count of 62 was wrong and by how much). Was 60 as of the spec's
    original count; the pre-merge review's blocker-4 commit added
    `logger.exception` to `app/api/request_context.py` afterward, and that
    hand-maintained number drifted wrong TWICE before anything pinned it
    exactly. Asserted EXACT rather than `>= 50` for that reason: a loose lower
    bound lets the count drift again with nothing to catch it. If this
    assertion fails, a log call was added or removed -- update the number
    below AND spec line 13 (`docs/superpowers/specs/
    2026-08-14-logging-kernel-design.md`) together, not just one of them.

    DROPPED TO 60, fix wave 6, M-12: `_OrgCycleFailures.contain` (app/worker.py)
    had an `if`/`else` with byte-identical `logger.error(...)` calls in both
    branches -- a leftover from when the two branches genuinely differed (one
    carried `exc_info`, the other didn't) that fix wave 5 made redundant
    without collapsing. Collapsing them to one unconditional call removes one
    call SITE, not one call at runtime -- every failing org still gets its own
    ERROR line; see `test_every_contained_failure_names_its_org_and_its_
    exception` in `tests/test_worker_sweep_containment.py`.

    BACK TO 61, platform health Task 1: `app/core/health/registry.py` logs one
    WARNING when a probe raises. That call is why a probe failure is
    diagnosable at all -- a `ProbeResult` carries a static reason CODE and
    never the exception, so this log line's `exc_info` is the only place the
    exception TYPE is recorded. This assertion firing is the mechanism working
    as intended: it forced the docstring and spec line 13 to be updated in the
    same change as the new call, which a `>= 50` drift budget failed to do
    twice.

    WAS 64, platform health Task 6: per-instance process heartbeats through
    Redis (`app/core/health/heartbeat.py`) are written by THREE processes --
    the API (`app/main.py`), the worker (`app/worker.py`), and the gateway
    (`app/gateway/main.py`) -- and each writer swallows and logs its own
    failure so a heartbeat write can never take down the process it reports
    on. That is three new ERROR call sites, one per writer, none of them a
    %-arg or an f-string: each logs a static message with `exc_info=True`,
    matching `ProbeRegistry._run_one`'s existing pattern above.

    NOW 65, the platform-heartbeat queued-task fix: the worker used to write
    its own liveness heartbeat from a beat-scheduled Celery task, sharing its
    task queue with every long sweep in this file -- a false signal that
    measured queue saturation, not process death. It now runs from a daemon
    thread the process starts once (`create_celery`'s `worker_process_init`
    receiver) and owns directly, matching how the API and gateway already
    write theirs. That thread's loop (`app.worker._worker_heartbeat_forever`)
    carries its OWN guard around each iteration's call to
    `write_worker_heartbeat` -- a second ERROR call site, deliberately not a
    duplicate of that coroutine's own: this loop has no external scheduler to
    retry it, so an exception this guard did not catch would silently end
    worker heartbeats for the rest of the process's life (see that
    function's docstring). One new call site, same shape as every other one
    here: a static message, `exc_info=True`, no %-arg, no f-string.

    WAS 65, error monitoring Task 1 (shipped without updating this number --
    the drift the docstring above already warned about, twice): `app/core/
    errors/capture.py`'s `capture_exception` logs one static ERROR with
    `exc_info=True` when recording a captured error itself fails, matching
    every other never-raise-swallow-and-log site here.

    NOW 67, error monitoring Task 2: `app/core/errors/store.py`'s drain logs
    one static WARNING (no `exc_info` -- nothing raised, a parse merely
    failed) when a correlation id it is draining to Postgres does not parse
    as a UUID, rather than let a malformed value crash the drain.

    NOW 69, error monitoring Task 3: two new call sites, both a single,
    shared definition each (not one per runtime install -- the AST sweep
    counts SOURCE sites, and each of these is installed into more than one
    process from the one place it is defined). `app.core.errors.
    asyncio_handler._handle` -- the replacement asyncio loop exception
    handler `app.main` and `app.gateway.main` both install (spec §6's first
    follow-up) -- logs one static ERROR, `exc_info` set only when the loop
    handed it a real exception, never the context dict asyncio's own default
    handler renders via `repr()`. `app.main.drain_platform_errors_once`
    logs one static ERROR with `exc_info=True` when a drain fails, matching
    every other never-raise-swallow-and-log site in this kernel -- the
    worker's own drain (`app.worker.drain_platform_errors_all`) deliberately
    does NOT swallow (see its docstring), so it adds no call site here.

    NOW 70, the API lifespan migration: `app.main.lifespan` replaced three
    `@app.on_event("startup")` handlers and, unlike them, has a shutdown
    half -- it cancels the heartbeat and error-drain tasks and waits a
    BOUNDED time for each. The one new site logs a static ERROR naming the
    task that outlived that bound. The %-arg is the task's own attribute
    name, a fixed string from a two-element tuple in that file, never
    operator or exception data. Caught by this assertion rather than by
    review: the change shipped with the count unchanged and the full suite
    is what found it.

    NOW 71, the identity admission gate: `app.core.identity.
    log_admission_refusal` is the single call site for a refusal at any of the
    three token-acceptance doors (`deps.py`, `api/v1/ws.py`,
    `modules/remote/tunnel.py`). ONE site rather than six because the doors
    would otherwise drift apart in wording and in what they carried, and a
    refusal is a security event that wants one shape. It logs a static WARNING
    with `door`, `sub`, `email` and `reason` in `extra` -- the address is a
    considered inclusion, not an oversight: it is not a credential, the same
    value already reaches the audit log in `auth.login`'s metadata, and the
    §3.3 lockout preflight prints exactly that column, so an operator
    diagnosing their own lockout has to be able to match the two.

    NOW 77, the invisible-identity-layer cycle: six new sites across the
    first-boot path (`app/core/setup_code.py`, `app/api/v1/setup.py`) and one
    in `app/core/identity`.

    WHY FIRST BOOT EARNS SIX. It is the one path with NO authenticated user, no
    session, and -- until the setup screen succeeds -- no console to look at. If
    the install code is wrong, the provisioning secret has not been written yet,
    or the claim half-fails, a log line is the ONLY evidence that exists.
    Everything else in this kernel can be reconstructed from the audit chain; a
    platform with zero users has no audit chain yet.

    All six are static templates with their variables in `extra`, and none
    carries the install code or any part of it -- `tests/test_setup_code.py`
    pins that separately, because a code leaked into a log is a code the
    operator's log shipper forwards somewhere it does not belong.

    NOW 91, subnet push runs (review item 9, 2026-09-30): one site in
    `modules/agent_push/runs.py::_record_dispatch_failure`, where one run
    target's dispatch fails after the pass already dispatched others and the
    pass stops. Static template; `extra` carries the run id, the target
    address and the cause's exception TYPE -- never its message, which may
    quote the signed parameters and so the secret.

    BEFORE THAT, 90, scheduling and health defaults: two sites, a first inventory
    collect that could not be queued (`modules/inventory`) and a baseline
    health profile that could not be seeded (`modules/health/baseline.py`).
    They name the device or org and nothing else.

    BEFORE THAT, 87, guacd retired (spec-screen-share.md phase 1): six sites went with
    the code that dialled guacd -- four in the media tunnel
    (`modules/remote/tunnel.py`) and two in the byte bridge (`broker.py`).

    BEFORE THAT, 93, screen share phase 0 (`docs/modules/remote-control/spec-screen-share.md`):
    thirteen sites. Seven in `modules/remote/screen_relay.py` (the gateway's
    relay: refusals by reason code, legs ending, a failed audit or end, the
    dropped-frame count at teardown), four in `modules/remote/screen_tunnel.py`
    (the api's viewer leg failing, a failed recording upload or end), one in
    `modules/remote/jobtypes.py` (an outcome string the server does not know)
    and one in `gateway/main.py` (the internal listener's address). They carry
    session ids, reason codes and counts; never the relay token, the viewer
    ticket or a frame, which is the user's screen.
    NOW 94, the agent self-update cycle: one site in
    `modules/agent_update/reconcile.py`, where one org's rollout tick fails
    and every other org is still ticked. It names the org and nothing else.

    NOW 106, MCP final review: one static ERROR in `app/mcp/server.py`, when
    the shielded `mcp.tool_called` audit write is abandoned at its deadline
    (a database that never answers). `extra` carries the tool name as the
    audit log would record it, the status and the deadline -- never the
    arguments. The rate limit's Redis WARNING moved from the handlers to the
    door (`_rate_limited`), one site either way.

    BEFORE THAT, 105, MCP connections polish: one more static WARNING in
    `app/mcp/api.py`, when the post-commit `mcp.app_revoke_incomplete` audit
    write fails. The revoke still answers 200 with its failure count; the line
    carries the org id and the client id (`extra=`) and `exc_info=True`.

    BEFORE THAT, 104, the MCP console admin API: two static WARNINGs in
    `app/mcp/api.py`, both after a revocation is committed and both with
    `exc_info=True` -- a person's AI-app consent could not be removed at
    Keycloak, or the removal ran out of time or found the provisioning client
    misconfigured. The response counts who was not reached. The review's fix
    round added the org id and client id as `extra=`; no user id or token is
    logged.

    BEFORE THAT, 102, MCP fix round 2: two more sites in `app/mcp/invoke.py`. A
    WARNING when a write that overran its caller's wait is cancelled at the
    hard cap, and an INFO when such a write finishes later on its own -- the
    tool name and the final status, so the outcome the person was told is
    "unknown" can be found afterwards. Nothing else.

    BEFORE THAT, 100, MCP fix round 1: three more sites. A write tool call that raised
    in the server's background task group (`app/mcp/invoke.py`; the tool
    name, `exc_info=True`), a tool call that raised anywhere in the handler
    (`app/mcp/server.py`, `logger.exception`, the tool name or `<invalid>`),
    and an INFO for a rate-limited call that is NOT audited because the
    window's first refusal already was (the person's id and the count).

    BEFORE THAT, 97, the MCP server's tool calls (`app/mcp/invoke.py`, `app/mcp/server.py`):
    four static WARNINGs. A tool call the API answered with a 5xx (the tool
    name and status), a 2xx that was not JSON (the same), a result the scrub
    blocked (the tool name and the field PATH -- a key name, never its value
    or the rest of the result), and the rate limit failing open on a Redis
    error (`exc_info=True`, nothing else). No argument, token or payload is
    passed to any of them.

    BEFORE THAT, 93, the language mirror (`app/api/v1/me.py::_mirror_locale`): one
    static WARNING with `exc_info=True`, when `PATCH /me` cannot write the saved
    language to the caller's Keycloak user. The save is already committed and
    the response says `identity_locale_synced: false`; the token and the user
    representation never reach the call.

    BEFORE THAT, 92, the Spanish server catalogue (`app/core/i18n/__init__.py::tr`):
    one site, a static WARNING with `exc_info=True`, when a catalogue value
    cannot be formatted with the parameters its English template takes. The
    request is answered in English instead of failing; the exception names
    the offending field, and no parameter or template text is logged.

    BEFORE THAT, 80, the device-groups rules cycle (`docs/modules/device-groups/`):
    one site in `services/device_groups.py`, where the rule evaluator meets an
    inventory snapshot Pydantic cannot parse. It counts that device as
    un-collected and carries on -- which is the right behaviour and is also
    invisible, so a group that quietly stopped matching a machine would look
    like a rule that no longer fits it. The line names the device and the org
    and nothing from the document, whose contents are the machine's own
    software and hardware.

    BEFORE THAT, 79, the assistant-ships-enabled cycle (`docs/modules/ai-admin/spec.md`
    §0.14): two sites in `create_app`, one per branch of "does this control
    plane have a model provider configured". They are there because that
    configuration became the ONLY switch on the AI assistant -- the per-org
    `enabled` flag was dropped by migration 0074 -- so an operator who edits
    `ITS_AI_*` has nowhere else to confirm the edit took effect. The configured
    branch names the provider key, kind and model and NOTHING ELSE: the base URL
    and the API key path stay out, because a log line is a place internal
    hostnames and secret paths go to be copied into tickets."""
    total = 0
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        total += sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_log_call(n)
        )
    assert total == 106, (
        f"the AST sweep found {total} log call sites, expected 106 -- if a log "
        "call was legitimately added or removed, update this number AND the "
        "running count in §2.2 of docs/superpowers/specs/"
        "2026-08-14-logging-kernel-design.md, together. NOT line 13: that is "
        "the count as measured BEFORE the kernel existed, and rewriting it "
        "erases the starting point that section describes -- editing it in "
        "place is how it came to claim 62, then 60, then 61, each wrong. See "
        "the note directly under it."
    )
