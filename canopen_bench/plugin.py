"""Extension API: device-/vendor-specific packages plug into the bench.

A plugin is a pip package that registers a ``BenchPlugin`` subclass under
the ``canopen_bench.plugins`` entry-point group::

    [project.entry-points."canopen_bench.plugins"]
    myvendor = "cob_myvendor.plugin:MyVendorPlugin"

Installing the package activates it — there is no configuration step. The
core stays vendor-neutral; everything a specific device family or machine
builder needs (adapter cards, EDS registry seeds, addressing flows,
firmware catalogs) arrives through these hooks. See docs/extending.md
for the guide; the docstrings below are the authoritative reference for
each hook's contract.
"""
from __future__ import annotations

import logging
from importlib import metadata
from pathlib import Path

from .values import Field

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "canopen_bench.plugins"


class AddressingProvider:
    """Vendor-specific addressing support the core cannot supply
    generically: the session identity that (re-)addressing runs broadcast
    and that flows reference as ``$session``. The core without a provider
    still addresses via the shipped standard-LSS flow — it just has no
    session identity, and flows using ``$session`` fail with a clear
    message."""

    name = "unnamed"

    def new_session(self, db) -> bytes:
        """Return the session identity bytes for one addressing run.
        Called once per run; ``db`` is the workspace ``Db`` for persistent
        state (master serial, sequence counters, ...)."""
        raise NotImplementedError


class DemoHook:
    """Simulates the device side of a vendor protocol on the demo bus, so
    vendor flows can run hardware-free. Hooks receive the ``EdsDemoBus``
    instance and use its small simulation API (``queue_raw``, ``renumber``,
    ``device_nodes``, ``set_nmt``, ``emit_emcy``, ``session``)."""

    name = "unnamed"

    def on_raw_frame(self, bus, cob: int, data: bytes) -> bool:
        """A flow sent a raw frame (``can_send``). Return True when this
        hook handled it — remaining hooks are skipped."""
        return False

    def press_button(self, bus) -> bool:
        """The operator pressed a demo device button (Setup page, demo mode
        only). Return True when handled."""
        return False


class TraceDecoder:
    """Interprets vendor-specific frames for the trace monitor — e.g. the
    button-teach telegrams 0x780/0x781/0x783 that generic CANopen decoding
    leaves blank."""

    name = "unnamed"

    def decode(self, cob: int, data: bytes) -> dict | None:
        """Return {dec?, obj?, val?} to fill the trace row's columns, or
        None when this decoder doesn't recognize the frame. Called for
        every frame that reaches the trace; keep it fast."""
        return None


class DevicePanel:
    """A panel for the sidebar, below the Devices box, for device families
    the core cannot know anything about: a front-panel mirror, virtual
    buttons, status LEDs. The core renders a declarative description and
    holds no device knowledge — which devices a panel applies to is
    entirely ``matches()``.

    Everything in the description is optional, because partial capability
    is the norm: a family whose display cannot be read over CAN
    contributes buttons and no canvas, one that only signals state
    contributes LEDs alone. A panel describing none of the three is not
    rendered.

    A panel that mirrors a physical device belongs to the real bus. On the
    demo adapter (``bench.demo``) there is no device behind the values, so
    such a panel should render nothing and leave the field to the core's
    own generic stand-in — a picture of a front panel assembled from
    made-up numbers is not a demo of anything. Panels that invent no
    measurement of their own are free to show up in demo mode.

    ``render()`` is called on every snapshot and must not touch the bus —
    it reads cached values (``bench.obj_vals``) and formats them. Bus
    access belongs in the plugin's own ``actions()``, triggered by the
    panel's buttons or its refresh action, so that showing a panel never
    turns into polling a device. A panel that raises is dropped for the
    rest of the session and logged once; it can never take a snapshot
    (and with it the whole UI) down.
    """

    #: namespaced as "<plugin name>.<key>" in the snapshot
    key = "unnamed"
    #: box heading, e.g. "Display"
    title = "Panel"

    def matches(self, dev: dict, eds: dict | None) -> bool:
        """True if this panel applies to ``dev`` — a row of ``bench.devices``
        ({node, name, sn, fw, eds, variant, nmt, ...}). ``eds`` is that
        device's registry row (``db.eds_list()``) or None when it has no
        EDS assigned."""
        return False

    def render(self, bench, dev: dict) -> dict | None:
        """The panel contents for the current snapshot, or None to show
        nothing this tick. Keys, all optional:

        ``canvas``  {w, h, fg?, bg?, draw: [...]} — a small vector display.
                    Primitives: {t: "line", p: [x1,y1,x2,y2], w?, c?},
                    {t: "poly", p: [x1,y1,x2,y2,...], fill?, w?, c?},
                    {t: "text", x, y, s, size?, tl?, c?}. ``tl`` squeezes
                    the glyphs into exactly that width, for a display whose
                    legends are printed into fixed cells — without it a
                    plugin laying out a row has to guess font metrics that
                    are the browser's to pick, and lands differently
                    elsewhere. ``c`` is "fg", "dim"
                    (fg at reduced opacity) or a literal CSS colour;
                    ``fg``/``bg`` are literal colours, since a physical
                    display's own colours do not follow the page theme.
                    Any primitive may carry ``blink``: "slow" or "fast" —
                    an element the device itself is flashing, the same
                    vocabulary as an LED's.
        ``buttons`` [{id, slot, label, title?}] — ``slot`` is "tl", "bl",
                    "tr" or "br", placing the button left or right of the
                    canvas. Clicking dispatches ``buttonAction`` with
                    {node, btn: id}; shift-click adds long: true.
        ``buttonAction`` the action name for the above, normally one of
                    this plugin's own namespaced actions.
        ``leds``    [{c, on, blink?, title?}] — ``c`` is a CSS colour,
                    ``on`` is True (lit), False (dark) or None (**not
                    readable**, rendered as a neutral ring, never as
                    dark). ``blink`` is "slow" or "fast".
        ``caption`` one line under the canvas — a mode, a screen name,
                    whatever the device says about itself that is not part
                    of the picture. Keep it out of the canvas: that one
                    mirrors the device, and text the device is not showing
                    has no business in it.
        ``refresh`` action name for the box's refresh control; omitted
                    means no refresh control.
        """
        return None


class StepType:
    """A flow/test-case step primitive contributed by a plugin, referenced
    in YAML as ``<plugin name>.<key>`` (e.g. ``- acme.block_download:
    {...}``). This is how vendor protocols that exceed the built-in
    primitives (block/segmented transfers, checksums, >8-byte sequences)
    become available to flows without core changes."""

    key = "unnamed"

    def validate(self, val) -> str | None:
        """Schema check at parse time; return an error text or None.
        Default: accept anything."""
        return None

    def label(self, val) -> str:
        """Progress line for the run display ("step 3/9 <label>")."""
        return self.key

    async def execute(self, bench, bus, node, val, regs, builtins) -> tuple[str, str]:
        """Run the step inside the shared VM. Return ("ok" | "fail" |
        "error" | "jump", reason/target) like the built-in primitives;
        resolve values via ``canopen_bench.core._resolve(value, regs,
        builtins)``. Exceptions are caught and turn into ERROR."""
        raise NotImplementedError


class SwdlStrategy:
    """Firmware-download implementation behind the SWDL page. The core
    ships a simulation (``core.SimSwdlStrategy``); a real vendor download
    protocol replaces it via ``BenchPlugin.swdl_strategy()``. All state
    lives on the bench (``swdl_run``/``swdl_done``/``swdl_prog``/
    ``fw_sel``/``swdl_mode``), so the UI stays snapshot-driven."""

    name = "unnamed"

    def start(self, bench) -> None:
        """Begin a download to ``bench.sel_devices`` — set ``swdl_run``,
        reset progress, log. Guards (already running, empty selection)
        happen before this is called."""
        raise NotImplementedError

    def step(self, bench) -> None:
        """Called once per tick (~0.8 s) while ``bench.swdl_run`` — drive
        ``bench.swdl_prog`` per node (0..100) and clear ``swdl_run`` /
        set ``swdl_done`` when finished."""
        raise NotImplementedError


class BenchPlugin:
    """Base class for extension packages. Every hook is optional and
    defaults to "contributes nothing" — subclasses override only what
    they provide. Hooks are read once at ``Bench`` construction."""

    #: short identifier, used in logs
    name = "unnamed"

    def adapters(self) -> list[dict]:
        """Extra adapter cards (same dict shape as ``data.ADAPTERS``).
        Plugin cards are listed before the built-in ones."""
        return []

    def adapter_backends(self) -> dict[str, tuple]:
        """Adapter key -> (python-can interface name, default channel) for
        the cards this plugin adds; merged into ``CanopenBus``'s mapping.
        The python-can backend itself ships separately via python-can's own
        ``can.interface`` entry-point group.

        A third element may carry a dict of further keyword arguments for
        the backend, for adapters the pair cannot describe (the built-in
        Vector entry needs ``app_name``). Pairs stay valid — nothing that
        worked before has to change."""
        return {}

    def seed_eds(self) -> list[dict]:
        """EDS registry rows ({file, dev, ident, code, enabled}) seeded
        once into an empty workspace registry — the files themselves are
        uploaded/copied by the user as usual.

        Two optional keys carry what the EDS panel would otherwise have to
        be told by hand, row by row:

        ``commands``
            The SDO commands offered for this device.
        ``variant``
            ``{index, sub, map?}`` — where the device keeps its variant
            number, so a scan reads it and the run report says which
            variant it ran against. Without ``map`` the value the device
            answers is the variant; with it, the answer is looked up
            there first. Every manufacturer puts this somewhere else,
            which is why it is per-row config and not an app-wide idea.
        """
        return []

    def firmware(self) -> list[dict]:
        """Firmware library entries ({ver, file, tag, meta}); listed
        before the core's demo entries."""
        return []

    def object_fields(self, symbols) -> dict[str, list[Field]]:
        """How to read an object's value symbolically: "0x2007:09" -> the
        fields packed into it (``canopen_bench/values.py``). A whole-value
        enum needs only a table name; anything nested — two fields in a
        byte, a byte lane, a flag register — is the same construct with a
        mask.

        ``symbols`` is the parsed symbol table, so a plugin can derive
        these from its headers instead of writing them out: a naming
        convention that ties a table to an object, a mask that the header
        itself names. That derivation is the plugin's business, not the
        core's — the convention belongs to whoever writes the headers.
        """
        return {}

    def describe_object(self, index: str, sub: str, symbols) -> str:
        """What the device's own firmware calls this object, or "".

        The bench already names an object from the EDS, which is what the
        operator sees on the device. This is the other name: the identifier
        in the headers, which is what somebody reading the firmware searches
        for. Where a plugin can supply it, the report step line carries it
        instead of the EDS name, because a test case is written against the
        firmware and its author is the person the line has to serve.

        Deriving it from a symbol table means knowing that vendor's naming
        convention (``eObj<index>_<sub>`` and the like), which is exactly
        what the neutral core must not assume — hence a hook rather than a
        rule. ``index`` and ``sub`` arrive as hex strings ("0x2345", "0x01").

        ``symbols`` is the parsed table the bench is actually working from —
        the workspace copy, which is the firmware under test rather than
        whatever the plugin was packaged with. Same argument as
        ``object_fields``, and for the same reason: an operator who dropped
        in newer headers expects the names to follow.
        """
        return ""

    def symbol_dirs(self) -> list[Path]:
        """Directories with the device's own C headers, parsed into symbol
        tables (indices, sub-indices, enum values — see
        ``canopen_bench/symbols.py``). Seeded into the workspace like flows,
        so the operator can drop in the headers of the firmware actually
        under test without waiting for a new plugin release."""
        return []

    def flow_dirs(self) -> list[Path]:
        """Directories with packaged flow files (*.yaml, format v2).
        Seeded into the workspace flows dir on startup; existing files
        are never overwritten, so vendor updates don't clobber local
        customizations."""
        return []

    def addressing_provider(self) -> AddressingProvider | None:
        """The session-identity provider for addressing runs, or None.
        With several plugins the first provider (entry-point order) wins."""
        return None

    def demo_hooks(self) -> list[DemoHook]:
        """Device-side protocol simulations to install on the demo bus."""
        return []

    def trace_decoders(self) -> list[TraceDecoder]:
        """Decoders for vendor-specific frames in the trace monitor."""
        return []

    def device_panels(self) -> list[DevicePanel]:
        """Sidebar panels for device families this plugin recognizes —
        front-panel mirrors, virtual buttons, status LEDs."""
        return []

    def emcy_codes(self) -> dict[int, str]:
        """Vendor-/profile-specific EMCY error-code texts, merged over the
        CiA-301 table (``data.EMCY_CODES``); on conflict the plugin text
        wins. Keys are 16-bit error codes — exact values or 0xXX00 class
        entries that catch every unlisted code of that class (lookup:
        exact, then ``code & 0xFF00``, then ``code & 0xF000``)."""
        return {}

    def actions(self, bench) -> dict[str, callable]:
        """Extra API actions, name -> handler(params). Dispatched as
        "<plugin name>.<action name>" so plugin actions can never collide
        with core act_* handlers. ``bench`` is the Bench instance the
        handlers may operate on."""
        return {}

    def step_types(self) -> list[StepType]:
        """Extra flow/test-case step primitives, referenced in YAML as
        "<plugin name>.<key>"."""
        return []

    def swdl_strategy(self) -> SwdlStrategy | None:
        """Replacement firmware-download implementation, or None to keep
        the core simulation. With several plugins the first one wins."""
        return None


def load_plugins() -> list[BenchPlugin]:
    """Discover and instantiate all installed plugins, sorted by entry-point
    name for a stable order. A broken plugin is logged and skipped — it must
    never take the bench down."""
    plugins: list[BenchPlugin] = []
    try:
        entry_points = sorted(metadata.entry_points(group=ENTRY_POINT_GROUP),
                              key=lambda ep: ep.name)
    except Exception:  # metadata backends misbehaving — run without plugins
        log.exception("plugin discovery failed — continuing without plugins")
        return plugins
    for ep in entry_points:
        try:
            plugins.append(ep.load()())
            log.info("loaded plugin %r (%s)", ep.name, ep.value)
        except Exception:
            log.exception("plugin %r failed to load — skipped", ep.name)
    return plugins
