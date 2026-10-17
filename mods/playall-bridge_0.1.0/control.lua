local directions = {north=defines.direction.north, east=defines.direction.east,
                    south=defines.direction.south, west=defines.direction.west}
local status_names = {}
for name, value in pairs(defines.entity_status) do status_names[value] = name end

local function distance(a, b)
  return math.sqrt((a.x-b.x)^2 + (a.y-b.y)^2)
end

local function contents(inventory)
  local out = {}
  if not inventory or not inventory.valid then return out end
  for key, item in pairs(inventory.get_contents()) do
    if type(item) == "table" then out[item.name] = (out[item.name] or 0) + item.count
    else out[key] = item end
  end
  return out
end

local function bot()
  if not storage.bot or not storage.bot.valid then error("工程师不存在或已经死亡。请开始新存档。") end
  return storage.bot
end

local finish

local function connected_player()
  for _, player in pairs(game.connected_players) do
    if storage.client_initialized and storage.client_initialized[player.index] then return player end
  end
end

local function watch_player(player)
  player.set_controller{type=defines.controllers.spectator}
  if player.connected then
    player.teleport(bot().position,bot().surface)
    player.zoom=1
    player.centered_on=bot()
  end
end

local function release_manual()
  local player=storage.manual_player and game.get_player(storage.manual_player)
  if player and player.connected then
    -- Return held items to the shared character before leaving its controller.
    player.clear_cursor()
    watch_player(player)
  end
  storage.manual_player=nil
end

local function take_manual(player)
  if not player or not player.connected then return end
  release_manual()
  player.set_controller{type=defines.controllers.character,character=bot()}
  storage.manual_player=player.index
  storage.paused=true
  game.speed=1
  game.tick_paused=not player.connected
end

local function connection()
  local player=connected_player()
  local owner=storage.manual_player and game.get_player(storage.manual_player)
  return {ok=true,connected=player~=nil,players=#game.connected_players,
          manual_control=owner~=nil and owner.connected and owner.controller_type==defines.controllers.character
                         and owner.character==bot(),
          character_id=bot().unit_number,paused=storage.paused}
end

local function setup(options)
  -- A dedicated shared character already has its starting kit. Avoid a second
  -- kit or a first-join cutscene taking control away from the handover.
  if remote.interfaces.freeplay then
    for method, value in pairs({set_disable_crashsite=true,set_skip_intro=true,set_created_items={}}) do
      if remote.interfaces.freeplay[method] then remote.call("freeplay",method,value) end
    end
  end
  storage.speed = options.speed or 4
  game.speed = storage.speed
  if not storage.bot or not storage.bot.valid then
    local surface = game.surfaces.nauvis or game.surfaces[1]
    surface.request_to_generate_chunks({0,0}, 5)
    surface.force_generate_chunk_requests()
    local spawn = surface.find_non_colliding_position("character", {0,0}, 32, 0.5)
    if not spawn then error("找不到安全的出生位置。") end
    storage.bot = surface.create_entity{name="character", position=spawn, force="player"}
    -- The vanilla freeplay starting kit, granted once to this separate character.
    for name, count in pairs({["iron-plate"]=8, wood=1, ["stone-furnace"]=1, ["burner-mining-drill"]=1}) do
      storage.bot.insert{name=name, count=count}
    end
    storage.bot.color = {r=0.15,g=0.55,b=0.95}
  end
  if storage.job then
    storage.bot.walking_state={walking=false,direction=defines.direction.north}
    storage.bot.mining_state={mining=false}
    storage.last_result={ok=false,message="上次退出时未完成的动作已中断；已保留存档中的进度。"}
    storage.job=nil
  end
  release_manual()
  storage.require_client=false
  storage.client_initialized={}
  storage.paused = true
  game.tick_paused = true
  return {ok=true, version=script.active_mods.base, bridge="0.1.0"}
end

local inventory_names = {
  fuel="fuel", output="furnace_result", chest="chest", lab="lab_input", ammo="turret_ammo", modules="assembling_machine_modules"
}
local function inventory_for(entity, purpose)
  local name = inventory_names[purpose]
  if purpose == "input" then
    name = entity.type == "furnace" and "furnace_source" or "assembling_machine_input"
  elseif purpose == "output" and entity.type ~= "furnace" then name = "assembling_machine_output" end
  if not name or not defines.inventory[name] then return nil end
  return entity.get_inventory(defines.inventory[name])
end

local function machine_info(entity)
  local entry = {name=entity.name, x=entity.position.x, y=entity.position.y, type=entity.type,
                 direction=entity.direction, status=status_names[entity.status], id=entity.unit_number,
                 contents=contents(entity.get_inventory(defines.inventory.chest))}
  entry.fuel = contents(inventory_for(entity,"fuel"))
  entry.input = contents(inventory_for(entity,"input"))
  entry.output = contents(inventory_for(entity,"output"))
  if entity.type == "assembling-machine" or entity.type == "furnace" then
    local recipe = entity.get_recipe()
    entry.recipe = recipe and recipe.name
    entry.progress = entity.crafting_progress
  end
  return entry
end

local function nearest(entities, position)
  table.sort(entities, function(a,b) return distance(a.position,position) < distance(b.position,position) end)
  return entities[1]
end

local function target_entity(a)
  local b = bot()
  local filter = {position={a.x or b.position.x,a.y or b.position.y}, radius=1}
  local entities = b.surface.find_entities_filtered(filter)
  local out = {}
  for _, entity in pairs(entities) do
    if entity ~= b and entity.type ~= "character" and entity.force == b.force then
      out[#out+1] = entity
    end
  end
  return nearest(out,filter.position.x and filter.position or {x=filter.position[1],y=filter.position[2]})
end

local function recipe_info(recipe)
  return {name=recipe.name, enabled=recipe.enabled, category=recipe.category, energy=recipe.energy,
          ingredients=recipe.ingredients, products=recipe.products}
end

local function observe()
  local b = bot()
  local p, s = b.position, b.surface
  local area = {{p.x-128,p.y-128},{p.x+128,p.y+128}}
  local patches, points = {}, {}
  local resources = s.find_entities_filtered{area=area,type="resource"}
  for i, entity in ipairs(resources) do
    local entry = patches[entity.name]
    local d = distance(p,entity.position)
    if not entry then
      entry = {name=entity.name, amount=0, distance=d,x=entity.position.x,y=entity.position.y}
      patches[entity.name] = entry
    end
    entry.amount = entry.amount + entity.amount
    if d < entry.distance then entry.x=entity.position.x; entry.y=entity.position.y; entry.distance=d end
    if i % 8 == 1 and #points < 1000 then points[#points+1] = {entity.position.x,entity.position.y,entity.name} end
  end
  local trees = s.find_entities_filtered{area=area,type="tree",limit=500}
  local tree = nearest(trees,p)
  if tree then patches.wood = {name="wood",amount=#trees,distance=distance(p,tree.position),x=tree.position.x,y=tree.position.y} end
  local patch_list = {}
  for _, entry in pairs(patches) do
    entry.distance = math.floor(entry.distance*10)/10
    patch_list[#patch_list+1] = entry
  end
  table.sort(patch_list,function(a,b) return a.distance<b.distance end)
  local machines, entities = {}, s.find_entities_filtered{area=area,force=b.force}
  table.sort(entities,function(a,c) return distance(a.position,p)<distance(c.position,p) end)
  for _, entity in pairs(entities) do
    if entity ~= b and entity.type ~= "character" and #machines<120 then machines[#machines+1] = machine_info(entity) end
  end
  local technologies = {}
  for name, tech in pairs(b.force.technologies) do
    if tech.enabled and not tech.researched and #technologies < 16 then
      local available = true
      for _, prerequisite in pairs(tech.prerequisites) do if not prerequisite.researched then available=false end end
      if available then technologies[#technologies+1] = {name=name,ingredients=tech.research_unit_ingredients} end
    end
  end
  local tiles = {}
  for y=-24,23 do
    for x=-24,23 do
      local tile = s.get_tile(p.x+x*4,p.y+y*4)
      tiles[#tiles+1] = tile.collides_with("water_tile") and 1 or 0
    end
  end
  local stats = b.force.get_item_production_statistics(s)
  local control=connection()
  return {ok=true,game="factorio",source="真实 Factorio",tick=game.tick,
          character_id=control.character_id,
          position={x=p.x,y=p.y},health=b.health,inventory=contents(b.get_main_inventory()),
          resources=patch_list,buildings=machines,technologies=technologies,
          research=b.force.current_research and b.force.current_research.name,
          research_progress=b.force.research_progress,iron_plates_produced=stats.get_input_count("iron-plate"),
          busy=storage.job~=nil,job=storage.job and {action=storage.job.a.action,phase=storage.job.phase},
          last_result=storage.last_result,paused=storage.paused,
          window_connected=control.connected,manual_control=control.manual_control,
          map={tiles=tiles,width=48,step=4,x=p.x-96,y=p.y-96,resources=points}}
end

finish=function(ok, message)
  local b = bot()
  b.walking_state = {walking=false,direction=defines.direction.north}
  b.mining_state = {mining=false}
  storage.last_result = {ok=ok,message=message,action=storage.job and storage.job.a.action,tick=game.tick}
  storage.job = nil
  game.tick_paused = true
end

local function perform(job)
  local b, a = bot(), job.a
  local inv, e = b.get_main_inventory(), job.entity
  if a.action == "move" then finish(true,"已走到目的地。")
  elseif a.action == "wait" then job.phase="waiting"; job.until_tick=game.tick+(a.seconds or 5)*60
  elseif a.action == "craft" then
    local recipe = b.force.recipes[a.name]
    if not recipe or not recipe.enabled then finish(false,"配方不存在或尚未解锁。"); return end
    local started = b.begin_crafting{recipe=a.name,count=a.count or 1,silent=true}
    if started == 0 then finish(false,"材料不足或此配方不能手工制作。"); return end
    job.phase="crafting"; job.started=started
  elseif a.action == "mine" or a.action == "deconstruct" then
    if not e or not e.valid then finish(false,"目标已不存在。"); return end
    job.phase="mining"; job.remaining=a.count or 1
    local properties=e.prototype.mineable_properties
    local product=properties and properties.products and properties.products[1]
    if not product or product.type~="item" then finish(false,"这个资源不能由工程师手工开采。"); return end
    job.product=product.name; job.start_count=inv.get_item_count(job.product)
    job.expected=(a.count or 1)*(product.amount or 1)
    b.selected=e
    b.mining_state={mining=true,position=e.position}
  elseif a.action == "build" then
    local item = prototypes.item[a.name]
    local prototype = item and item.place_result
    if not prototype then finish(false,"这个物品不能建造。"); return end
    if inv.get_item_count(a.name)<1 then finish(false,"背包里没有这个建筑。"); return end
    local spec = {name=prototype.name,position={a.x,a.y},force=b.force,direction=directions[a.direction or "north"],build_check_type=defines.build_check_type.manual}
    if not b.surface.can_place_entity(spec) then finish(false,"该位置放不下建筑，请检查水面、树木、矿区或建筑碰撞。"); return end
    spec.build_check_type=nil; spec.raise_built=true
    local built = b.surface.create_entity(spec)
    if not built then finish(false,"游戏拒绝建造。"); return end
    inv.remove{name=a.name,count=1}
    finish(true,"已建造 "..built.name.."。")
  elseif a.action == "insert" or a.action == "extract" then
    if not e or not e.valid then finish(false,"没有找到建筑。"); return end
    if e.force ~= b.force then finish(false,"只能操作己方建筑。"); return end
    local container = a.inventory and inventory_for(e,a.inventory) or e
    if not container then finish(false,"该建筑没有指定的物品栏。"); return end
    local count = a.count or 1
    if a.action == "insert" then
      count=math.min(count,inv.get_item_count(a.name))
      if count<1 then finish(false,"背包里没有这个物品。"); return end
      local inserted=container.insert{name=a.name,count=count}
      inv.remove{name=a.name,count=inserted}
      finish(inserted>0,"放入 "..inserted.." 个 "..a.name.."。")
    else
      count=math.min(count,container.get_item_count(a.name))
      if count<1 then finish(false,"建筑里没有这个物品。"); return end
      if not inv.can_insert{name=a.name,count=count} then finish(false,"背包空间不足。"); return end
      local removed
      if a.inventory then removed=container.remove{name=a.name,count=count}
      else removed=e.remove_item{name=a.name,count=count} end
      inv.insert{name=a.name,count=removed}
      finish(removed>0,"取出 "..removed.." 个 "..a.name.."。")
    end
  elseif a.action == "set_recipe" then
    local recipe=b.force.recipes[a.name]
    if not e or e.type~="assembling-machine" then finish(false,"请选择组装机。"); return end
    if not recipe or not recipe.enabled or not e.prototype.crafting_categories[recipe.category] then finish(false,"配方未解锁或这台机器不支持。"); return end
    local returned=e.set_recipe(a.name)
    for _, item in pairs(returned) do
      local inserted=inv.insert(item)
      if inserted<item.count then b.surface.spill_item_stack{position=b.position,stack={name=item.name,count=item.count-inserted},enable_looted=true,force=b.force} end
    end
    finish(true,"已设置配方。")
  elseif a.action == "rotate" then
    finish(e and e.valid and e.rotate(),"已请求旋转建筑。")
  elseif a.action == "research" then
    local tech=b.force.technologies[a.name]
    if not tech or tech.researched then finish(false,"科技不存在或已研究。"); return end
    for _, pre in pairs(tech.prerequisites) do if not pre.researched then finish(false,"前置科技未完成。"); return end end
    local added=b.force.add_research(a.name)
    finish(added,added and "已加入科研队列；需要实验室与科技包。" or "未能开始这项科研，请检查科技是否已解锁及科研队列。")
  else finish(false,"未知动作。") end
end

local function dispatch(a)
  if a.action == "observe" then return observe() end
  if a.action == "recipes" then
    local out={}
    for name,recipe in pairs(bot().force.recipes) do
      if recipe.enabled and (not a.name or string.find(name,a.name,1,true)) and #out<35 then out[#out+1]=recipe_info(recipe) end
    end
    return {ok=true,recipes=out}
  end
  if a.action == "inspect" then
    if a.name and not a.x then
      local recipe=bot().force.recipes[a.name]
      return recipe and {ok=true,recipe=recipe_info(recipe)} or {ok=false,error="找不到配方。"}
    end
    local e=target_entity(a)
    return e and {ok=true,entity=machine_info(e)} or {ok=false,error="该位置没有建筑。"}
  end
  if storage.require_client and not connected_player() then
    return {ok=false,reason="window_closed",error="游戏窗口未连接，AI 不会执行动作。"}
  end
  if storage.paused and storage.manual_player then
    return {ok=false,error="玩家正在接手角色；请先在 PlayALL 继续 AI。"}
  end
  if storage.job then return {ok=false,error="上一个动作尚未结束。"} end
  local b=bot()
  local e, target
  if a.action == "mine" then
    local filter={position=b.position,radius=128}
    if a.name=="wood" or a.name=="tree" then filter.type="tree" else filter.name=a.name; filter.type="resource" end
    if a.x and a.y then filter.position={a.x,a.y}; filter.radius=4 end
    e=nearest(b.surface.find_entities_filtered(filter),b.position)
    if not e then return {ok=false,error="视野内没有这种资源，请先移动或观察。"} end
    target=e.position
  elseif a.action=="move" or a.action=="build" then target={x=a.x,y=a.y}
  elseif a.action=="insert" or a.action=="extract" or a.action=="set_recipe" or a.action=="rotate" or a.action=="deconstruct" then
    e=target_entity(a)
    if not e then return {ok=false,error="该位置没有可操作的建筑。"} end
    target=e.position
  end
  storage.last_result=nil
  storage.job={a=a,entity=e,started_tick=game.tick,phase="starting"}
  storage.paused=false; game.tick_paused=false
  local radius=a.action=="move" and 0.5 or 2.4
  if target and distance(b.position,target)>radius then
    storage.job.phase="pathfinding"
    storage.job.path_id=b.surface.request_path{
      bounding_box=b.prototype.collision_box,collision_mask=b.prototype.collision_mask,
      start=b.position,goal=target,force=b.force,radius=radius,
      entity_to_ignore=b,pathfind_flags={cache=false,low_priority=false}}
  else perform(storage.job) end
  return {ok=true,busy=storage.job~=nil,result=storage.last_result}
end

script.on_init(function() storage.paused=true end)
script.on_event(defines.events.on_player_created,function(event)
  local player=game.get_player(event.player_index)
  if player and storage.bot and storage.bot.valid then
    -- Creation may precede connection. Keep ticks running until the joined
    -- event confirms that the client can actually control the shared character.
    watch_player(player)
  end
end)
script.on_event(defines.events.on_player_joined_game,function(event)
  local player=game.get_player(event.player_index)
  if player and storage.bot and storage.bot.valid then
    storage.client_initialized=storage.client_initialized or {}
    storage.client_initialized[player.index]=true
    if storage.paused and not storage.manual_player then take_manual(player) else watch_player(player) end
  end
end)
script.on_event(defines.events.on_pre_player_left_game,function(event)
  -- Detach before Factorio logs off the player, so the shared character and
  -- its inventory remain in the world rather than in a disconnected avatar.
  if storage.manual_player==event.player_index then release_manual() end
  if storage.require_client then
    local another=false
    for _, player in pairs(game.connected_players) do
      if player.index~=event.player_index then another=true end
    end
    if not another then
      if storage.job then finish(false,"游戏窗口已断开，当前 AI 动作已中止。") end
      storage.paused=true
      game.tick_paused=true
    end
  end
end)
script.on_event(defines.events.on_player_left_game,function(event)
  if storage.client_initialized then storage.client_initialized[event.player_index]=nil end
  if storage.require_client and not connected_player() then
    if storage.job then finish(false,"游戏窗口已断开，当前 AI 动作已中止。") end
    release_manual()
    storage.paused=true
    game.tick_paused=true
  elseif storage.paused and not storage.manual_player then
    release_manual()
    local player=connected_player()
    if player then take_manual(player) else game.tick_paused=true end
  end
end)
script.on_event(defines.events.on_script_path_request_finished,function(event)
  local job=storage.job
  if not job or job.path_id~=event.id then return end
  if not event.path then finish(false,"找不到通路。尝试更近的位置，或先清理树木。"); return end
  job.path=event.path; job.waypoint=1; job.phase="walking"
end)

script.on_event(defines.events.on_tick,function()
  local job=storage.job
  if not job or storage.paused then return end
  local ok,err=pcall(function()
    local b=bot()
    if game.tick-job.started_tick>60*240 then finish(false,"动作超过 240 游戏秒，已停止。"); return end
    if job.phase=="walking" then
      local point=job.path[job.waypoint]
      if not point then b.walking_state={walking=false,direction=defines.direction.north}; perform(job); return end
      if distance(b.position,point.position)<0.45 then job.waypoint=job.waypoint+1; return end
      local dx,dy=point.position.x-b.position.x,point.position.y-b.position.y
      local angle=math.atan2(dx,-dy)
      local index=math.floor(angle/(math.pi/4)+0.5)%8
      local ds={defines.direction.north,defines.direction.northeast,defines.direction.east,defines.direction.southeast,
                defines.direction.south,defines.direction.southwest,defines.direction.west,defines.direction.northwest}
      b.walking_state={walking=true,direction=ds[index+1]}
    elseif job.phase=="mining" then
      if not job.entity or not job.entity.valid then finish(true,"目标已采集完毕。"); return end
      if distance(b.position,job.entity.position)>3.5 then finish(false,"距离资源太远。"); return end
      if b.get_main_inventory().get_item_count(job.product)-job.start_count>=job.expected then finish(true,"采集完成。") end
    elseif job.phase=="crafting" and b.crafting_queue_size==0 then finish(true,"手工制作完成，已启动 "..job.started.." 次配方。")
    elseif job.phase=="waiting" and game.tick>=job.until_tick then finish(true,"等待完成。") end
  end)
  if not ok then finish(false,tostring(err)) end
end)

remote.add_interface("playall",{
  setup=setup,
  dispatch=function(a)
    local ok,result=pcall(dispatch,a)
    if not ok then
      if storage.job then finish(false,tostring(result)) end
      return {ok=false,error=tostring(result)}
    end
    return result
  end,
  connection=function(options)
    if options then
      if options.required~=nil then storage.require_client=options.required end
      if options.waiting and not connected_player() then storage.paused=true; game.tick_paused=false end
    end
    return connection()
  end,
  pause=function()
    local player=connected_player()
    local replan=player~=nil or storage.require_client
    if replan and storage.job then finish(false,"AI 已暂停，当前动作已中止；继续时会重新观察。") end
    storage.paused=true
    if player then take_manual(player) else game.tick_paused=true end
    local result=connection()
    result.replan=replan or false
    return result
  end,
  resume=function()
    local replan=storage.manual_player~=nil
    release_manual()
    for _, player in pairs(game.connected_players) do watch_player(player) end
    storage.paused=false
    game.speed=storage.speed or 1
    game.tick_paused=storage.job==nil
    return {ok=true,replan=replan}
  end,
  cancel=function()
    if storage.job then finish(false,"动作已停止。") end
    release_manual()
    storage.paused=true; game.tick_paused=true; return {ok=true}
  end
})
