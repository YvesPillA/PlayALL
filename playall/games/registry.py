"""Installed adapters supply their own factory, tools, validator and rules.

The runner and desktop never branch on game IDs. AI output cannot load code.
"""
from dataclasses import dataclass
from importlib import import_module


def resolve(path):
    module, name = path.split(":", 1)
    return getattr(import_module(module), name)


@dataclass(frozen=True)
class GameSpec:
    id: str
    label: str
    factory: str
    rules: str
    tool_factory: str = "playall.games.actions:tools_for_game"
    validator: str = "playall.games.actions:validate_action"
    has_game_window: bool = True
    path_setting: str = ""
    executable_name: str = ""
    process_attach: bool = False
    process_verifier: str = ""
    connection_preparer: str = ""
    connection_instructions: str = ""

    def create(self, directory, settings, emit):
        return resolve(self.factory)(directory, settings, emit)

    def tools(self):
        return resolve(self.tool_factory)()

    def validate(self, action):
        return resolve(self.validator)(action)

    def accepts_process(self, process):
        return bool(self.process_attach and process.name.casefold() == self.executable_name.casefold()
                    and self.process_verifier and resolve(self.process_verifier)(process))

    def prepare_connection(self, directory, path, emit):
        if not self.connection_preparer:
            raise ValueError("这个游戏不需要准备连接组件。")
        return resolve(self.connection_preparer)(directory, path, emit)


_games = {}


def register_game(spec):
    if not spec.id or not spec.id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("适配器 ID 只能包含字母、数字、下划线和连字符。")
    if spec.id in _games:
        raise ValueError("适配器 ID 已存在。")
    _games[spec.id] = spec


def game_specs():
    return dict(_games)


register_game(GameSpec(
    "factorio", "Factorio", "playall.games.factorio_attach:Factorio",
    "Factorio: control the existing human player's character in a loaded SINGLE-PLAYER save. "
    "Connecting never creates a map or character, grants items, changes speed, or starts another server. "
    "Use real inventory, collision, mining, crafting and machine production. "
    "mine and build automatically walk into reach. Handcrafting cannot smelt ore: use a furnace, insert coal into fuel "
    "and ore into input, wait for game time, then extract output. Consult inspect(name=recipe) and recipes for unlocked recipes. "
    "Technology triggers may update shortly after a craft finishes; briefly wait and inspect again before using a newly unlocked recipe. "
    "build direction is the entity's game orientation. Belts/drills output in that direction; inserters pick up from that direction and drop behind them. "
    "Before building, move outside the target's full footprint; automatic approach only checks reach and your own character can block placement. "
    "If placement fails, observe the current position and obstructions, then move to a clear side before retrying. "
    "Inspect pickup_position/drop_position and belt_lines to verify the actual transport layout. Burner drills drop onto the adjacent tile and have no output inventory. "
    "Place a furnace or chest at the output. Counts are recipe or mining cycles. Coordinates and prototype names come from observations. "
    "Build sustainable automation, recover from failed placement, and progress through technology. "
    "Inventory and item transfers currently use normal quality. Pause returns manual control without switching controllers. "
    "Historical notes may refer to a different save: trust the current observation for positions, inventory and progress. "
    "You have no tool to create/load/delete saves, exit the game, or operate its main menu: ask the human to do those. "
    "The app handles periodic saves; never try to save via an action name. Factorio 2.0.77 Windows Steam is required.",
    path_setting="factorio_path", executable_name="factorio.exe", process_attach=True,
    process_verifier="playall.games.factorio_attach:is_supported_process",
    connection_preparer="playall.games.factorio_attach:prepare_connection",
    connection_instructions="Factorio 请先进入单人地图再连接；主菜单尚不能连接。首次使用或更新组件时，先保存并关闭游戏，再准备连接组件。",
))
register_game(GameSpec(
    "aistalt", "幼神 Demo", "playall.games.aistalt:Aistalt",
    "Aistalt / Infant God Demo is a narrative and management game, not Factorio. Read the visible text, dialogue, "
    "enabled controls, costs and options. All actions use the game's existing UI event chain. Use exact observed "
    "control ids and view_revision. Use one mutating action per response and observe the result; never guess ids, "
    "force disabled options, change game variables, grant items, bypass dice/skill checks or invent success. "
    "advance operates the visible Btn_NextLine; it can first finish typing, so inspect before advancing again. "
    "Use scroll to reveal more real controls and wait while loading or animating. Clicks in lists retain their "
    "native selection rules. New games, loading saves, overwriting a slot and returning to a menu need "
    "the human's specific confirmation in the chat pane. A requires_confirmation control cannot be authorized by the model. "
    "Do not repeatedly quick-save: the app saves periodically after progress and on pause. Never independently delete saves, "
    "quit the game or open external links. If the story reaches an ending, remember it and explore available "
    "gameplay without deleting progress. The human can take over while AI is paused.",
    tool_factory="playall.games.aistalt_actions:tools_for_game",
    validator="playall.games.aistalt_actions:validate_action",
    path_setting="aistalt_path", executable_name="Aistalt.exe", process_attach=True,
    process_verifier="playall.games.aistalt:is_supported_process",
    connection_preparer="playall.games.aistalt:prepare_connection",
))
register_game(GameSpec(
    "sandbox", "采矿练习场", "playall.games.sandbox:Sandbox",
    "This is a separate small mining practice game, not Factorio. It supports mining, crafting, building furnaces, "
    "fuel/input/output transfers and waiting for smelting. Do not claim to be running the Factorio engine.",
    has_game_window=False
))
