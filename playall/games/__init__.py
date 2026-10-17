def make_game(game_id, directory, settings, emit):
    from .registry import game_specs
    try:
        spec = game_specs()[game_id]
    except KeyError:
        raise ValueError("这个游戏还没有安装适配器。") from None
    return spec.create(directory, settings, emit)
