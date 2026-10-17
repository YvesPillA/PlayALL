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
    if type(item) == "table" then
      if item.quality == "normal" or item.quality == nil then out[item.name] = (out[item.name] or 0) + item.count end
    else out[key] = item end
  end
  return out
end

-- Protocol 2: one human in an existing single-player game.
-- Local state deliberately never survives loading another save. No player,
-- character, items, technology, speed or world is created by this mod.
local state = {paused=true, cache={}, order={}}
local finish
local halt

local function bot()
  local p = state.player and game.get_player(state.player)
  if not p or not p.valid or not p.connected or not p.character or not p.character.valid
     or p.controller_type ~= defines.controllers.character or p.character.unit_number ~= state.character then
    error("当前玩家角色尚未就绪；请亲自进入地图或完成重生。")
  end
  return p
end

local function ready_player()
  if game.is_multiplayer() or #game.connected_players ~= 1 then return nil end
  for _,p in pairs(game.connected_players) do
    if p.character and p.character.valid and p.controller_type == defines.controllers.character then return p end
  end
end

local function connection()
  local p = ready_player()
  return {ok=true,connected=p~=nil,paused=state.paused,
          character_id=p and p.character.unit_number,manual_control=state.paused,
          version=script.active_mods.base,bridge="0.2.2",protocol=2,
          multiplayer=game.is_multiplayer()}
end

local function inventory_for(entity, purpose)
  -- defines.inventory values overlap between entity types. An arbitrary enum
  -- can return a real but completely different inventory (a chest as fuel).
  if purpose == "fuel" then return entity.get_fuel_inventory() end
  if purpose == "output" then
    -- The generic getter also treats a container's contents as its output.
    if ({furnace=true,["assembling-machine"]=true,["rocket-silo"]=true,["agricultural-tower"]=true,
         ["asteroid-collector"]=true})[entity.type] then return entity.get_output_inventory() end
    return nil
  end
  if purpose == "modules" then return entity.get_module_inventory() end
  local kind, name = entity.type
  if purpose == "input" then
    name = ({furnace="furnace_source",["assembling-machine"]="assembling_machine_input",
             ["rocket-silo"]="rocket_silo_input",["agricultural-tower"]="agricultural_tower_input"})[kind]
  elseif purpose == "chest" then
    name = ({container="chest",["logistic-container"]="chest",["linked-container"]="linked_container_main",
             ["cargo-wagon"]="cargo_wagon",car="car_trunk",["spider-vehicle"]="spider_trunk",
             ["cargo-landing-pad"]="cargo_landing_pad_main"})[kind]
  elseif purpose == "lab" and kind == "lab" then name = "lab_input"
  elseif purpose == "ammo" then
    name = ({["ammo-turret"]="turret_ammo",["artillery-turret"]="artillery_turret_ammo",
             ["artillery-wagon"]="artillery_wagon_ammo",car="car_ammo",["spider-vehicle"]="spider_ammo"})[kind]
  end
  if not name or not defines.inventory[name] then return nil end
  return entity.get_inventory(defines.inventory[name])
end

local function extract_items(player, source, name, requested, source_is_inventory)
  local destination = player.get_main_inventory()
  local count = math.min(requested, source.get_item_count(name), destination.get_insertable_count(name))
  if count < 1 then return 0, 0 end
  local stack = {name=name, count=count, quality="normal"}
  local removed = source_is_inventory and source.remove(stack) or source.remove_item(stack)
  local inserted = destination.insert{name=name, count=removed, quality="normal"}
  -- Defensive compensation if an unusual stack/filter makes the capacity
  -- estimate optimistic. Never discard items removed from the source.
  local dropped = 0
  if inserted < removed then
    local returned = source.insert{name=name, count=removed-inserted, quality="normal"}
    dropped = removed-inserted-returned
    if dropped > 0 then
      player.surface.spill_item_stack{position=player.position, stack={name=name,count=dropped,quality="normal"},
                                     enable_looted=true, force=player.force}
    end
  end
  return inserted, dropped
end

local function carried_count(player, inventory, name)
  local count=inventory.get_item_count(name)
  local cursor=player.cursor_stack
  if cursor.valid_for_read and cursor.name==name and cursor.quality.name=="normal" then count=count+cursor.count end
  return count
end

local function build_confirmed(player, inventory, name, prototype_name, position, before_count)
  local placed=player.surface.find_entity(prototype_name,position)
  -- Native cursor auto-refill may already hold the next belt/pole. Success is
  -- an actual friendly entity plus consumption of exactly one carried item.
  return placed~=nil and placed.valid and placed.force==player.force
     and carried_count(player,inventory,name)==before_count-1
end

local function machine_info(entity)
  local entry = {name=entity.name, x=entity.position.x, y=entity.position.y, type=entity.type,
                 direction=entity.direction, status=status_names[entity.status], id=entity.unit_number,
                 contents=contents(inventory_for(entity,"chest")), energy=entity.energy,
                 electric_network_id=entity.electric_network_id}
  for _, purpose in ipairs{"fuel","input","output","lab","ammo","modules"} do
    entry[purpose] = contents(inventory_for(entity,purpose))
  end
  entry.fluids = {}
  for index=1,#entity.fluidbox do
    local fluid = entity.fluidbox[index]
    if fluid then entry.fluids[#entry.fluids+1] = {index=index,name=fluid.name,amount=fluid.amount,temperature=fluid.temperature} end
  end
  if entity.type == "assembling-machine" or entity.type == "furnace" then
    local recipe = entity.get_recipe()
    entry.recipe = recipe and recipe.name
    entry.progress = entity.crafting_progress
  end
  if entity.type == "mining-drill" or entity.type == "inserter" then entry.drop_position=entity.drop_position end
  if entity.type == "inserter" then
    entry.pickup_position=entity.pickup_position
    local stack=entity.held_stack
    if stack.valid_for_read then entry.held_stack={name=stack.name,count=stack.count,quality=stack.quality.name} end
  end
  if entity.type == "transport-belt" then
    entry.belt_lines={contents(entity.get_transport_line(1)),contents(entity.get_transport_line(2))}
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
    if entity ~= b.character and entity.type ~= "character" and entity.force == b.force then
      out[#out+1] = entity
    end
  end
  return nearest(out,filter.position.x and filter.position or {x=filter.position[1],y=filter.position[2]})
end

local function recipe_info(recipe)
  return {name=recipe.name, enabled=recipe.enabled, hidden=recipe.hidden, category=recipe.category, energy=recipe.energy,
          ingredients=recipe.ingredients, products=recipe.products}
end

local function technology_info(tech)
  local prerequisites={}
  for name, prerequisite in pairs(tech.prerequisites) do prerequisites[name]=prerequisite.researched end
  return {name=tech.name,enabled=tech.enabled,researched=tech.researched,prerequisites=prerequisites,
          ingredients=tech.research_unit_ingredients,count=tech.research_unit_count,
          energy=tech.research_unit_energy,trigger=tech.prototype.research_trigger}
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
    if entity ~= b.character and entity.type ~= "character" and #machines<120 then machines[#machines+1] = machine_info(entity) end
  end
  local technologies = {}
  for name, tech in pairs(b.force.technologies) do
    if tech.enabled and not tech.researched and #technologies < 16 then
      local available = true
      for _, prerequisite in pairs(tech.prerequisites) do if not prerequisite.researched then available=false end end
      if available then technologies[#technologies+1] = technology_info(tech) end
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
  return {ok=true,game="factorio",source="真实 Factorio",tick=game.tick,bridge="0.2.2",
          character_id=control.character_id,
          position={x=p.x,y=p.y},health=b.character.health,inventory=contents(b.get_main_inventory()),
          resources=patch_list,buildings=machines,technologies=technologies,crafting_queue=b.crafting_queue,
          research=b.force.current_research and b.force.current_research.name,
          research_progress=b.force.research_progress,iron_plates_produced=stats.get_input_count("iron-plate"),
          production={iron_ore=stats.get_input_count("iron-ore"),copper_ore=stats.get_input_count("copper-ore"),
                      iron_plate=stats.get_input_count("iron-plate"),copper_plate=stats.get_input_count("copper-plate")},
          busy=state.job~=nil,job=state.job and {action=state.job.a.action,phase=state.job.phase},
          last_result=state.last_result,paused=state.paused,
          window_connected=control.connected,manual_control=control.manual_control,surface=s.name,
          map={tiles=tiles,width=48,step=4,x=p.x-96,y=p.y-96,resources=points}}
end

finish=function(ok, message)
  pcall(function()
    local b = bot()
    b.walking_state = {walking=false,direction=defines.direction.north}
    b.mining_state = {mining=false}
  end)
  state.last_result = {ok=ok,message=message,action=state.job and state.job.a.action,tick=game.tick}
  state.job = nil
  -- The human world keeps running.
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
    b.update_selected_entity(e.position)
    b.mining_state={mining=true,position=e.position}
  elseif a.action == "build" then
    local item = prototypes.item[a.name]
    local prototype = item and item.place_result
    if not prototype then finish(false,"这个物品不能建造。"); return end
    if inv.get_item_count(a.name)<1 then finish(false,"背包里没有这个建筑。"); return end
    local spec = {name=prototype.name,position={a.x,a.y},force=b.force,direction=directions[a.direction or "north"],build_check_type=defines.build_check_type.manual}
    if not b.surface.can_place_entity(spec) then finish(false,"该位置放不下建筑，请检查水面、树木、矿区或建筑碰撞。"); return end
    if b.cursor_stack.valid_for_read then finish(false,"请先清空角色手持物品。"); return end
    if not b.cursor_stack.set_stack{name=a.name,count=1} then finish(false,"不能拿起这个物品。"); return end
    if inv.remove{name=a.name,count=1}~=1 then b.cursor_stack.clear(); finish(false,"背包物品已变化。"); return end
    local params={position={a.x,a.y},direction=directions[a.direction or "north"]}
    local before_count=carried_count(b,inv,a.name)
    local ok,result=pcall(function()
      if not b.can_build_from_cursor(params) then return false end
      b.build_from_cursor(params)
      return build_confirmed(b,inv,a.name,prototype.name,params.position,before_count)
    end)
    -- Clearing the native cursor returns an unconsumed item to the inventory.
    b.clear_cursor()
    if not ok then finish(false,tostring(result)); return end
    finish(result==true,result and "已建造 "..prototype.name.."。" or "游戏拒绝建造。")
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
      if container.get_item_count(a.name)<1 then finish(false,"建筑里没有这个物品。"); return end
      local inserted,dropped=extract_items(b,container,a.name,count,a.inventory~=nil)
      local message=inserted>0 and ("取出 "..inserted.." 个 "..a.name.."。") or "背包空间不足。"
      if dropped>0 then message=message.."背包变化，另有 "..dropped.." 个保留在角色脚边。" end
      finish(inserted>0,message)
    end
  elseif a.action == "set_recipe" then
    local recipe=b.force.recipes[a.name]
    if not e or e.type~="assembling-machine" then finish(false,"请选择组装机。"); return end
    if not recipe or not recipe.enabled or not e.prototype.crafting_categories[recipe.category] then finish(false,"配方未解锁或这台机器不支持。"); return end
    local returned=e.set_recipe(a.name)
    for _, item in pairs(returned) do
      local inserted=inv.insert(item)
      if inserted<item.count then b.surface.spill_item_stack{position=b.position,stack={name=item.name,count=item.count-inserted,quality=item.quality},enable_looted=true,force=b.force} end
    end
    finish(true,"已设置配方。")
  elseif a.action == "rotate" then
    finish(e and e.valid and e.rotate(),"已请求旋转建筑。")
  elseif a.action == "research" then
    local tech=b.force.technologies[a.name]
    if not tech or tech.researched then finish(false,"科技不存在或已研究。"); return end
    if not tech.enabled then finish(false,"这项科技当前不可研究。"); return end
    for _, pre in pairs(tech.prerequisites) do if not pre.researched then finish(false,"前置科技未完成。"); return end end
    if tech.prototype.research_trigger then
      finish(false,"这项科技由游戏行为自动解锁，请 inspect 科技名称查看 trigger 并完成条件；无需加入科研队列。")
      return
    end
    local added=b.force.add_research(a.name)
    finish(added,added and "已加入科研队列；需要实验室与科技包。" or "未能开始这项科研，请检查科技是否已解锁及科研队列。")
  else finish(false,"未知动作。") end
end

local function dispatch(a)
  if a.action == "observe" then return observe() end
  if a.action == "recipes" then
    local out={}
    for name,recipe in pairs(bot().force.recipes) do
      if recipe.enabled and not recipe.hidden and (not a.name or string.find(name,a.name,1,true)) and #out<35 then out[#out+1]=recipe_info(recipe) end
    end
    return {ok=true,recipes=out}
  end
  if a.action == "inspect" then
    if a.name and not a.x then
      local recipe=bot().force.recipes[a.name]
      local tech=bot().force.technologies[a.name]
      if not recipe and not tech then return {ok=false,error="找不到配方或科技。"} end
      return {ok=true,recipe=recipe and recipe_info(recipe),technology=tech and technology_info(tech)}
    end
    local e=target_entity(a)
    return e and {ok=true,entity=machine_info(e)} or {ok=false,error="该位置没有建筑。"}
  end
  if not state.session or state.paused then
    return {ok=false,error="AI 尚未接手或已经暂停。"}
  end
  if state.job then return {ok=false,error="上一个动作尚未结束。"} end
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
  state.last_result=nil
  state.job={a=a,entity=e,started_tick=game.tick,phase="starting"}
  state.paused=false
  local radius=a.action=="move" and 0.5 or 2.4
  if target and distance(b.position,target)>radius then
    state.job.phase="pathfinding"
    state.job.path_id=b.surface.request_path{
      bounding_box=b.character.prototype.collision_box,collision_mask=b.character.prototype.collision_mask,
      start=b.position,goal=target,force=b.force,radius=radius,
      entity_to_ignore=b.character,pathfind_flags={cache=false,low_priority=false}}
  else perform(state.job) end
  return {ok=true,busy=state.job~=nil,result=state.last_result}
end


script.on_event(defines.events.on_script_path_request_finished,function(event)
  local job=state.job
  if not job or job.path_id~=event.id then return end
  if not event.path then finish(false,"找不到通路。尝试更近的位置，或先清理树木。"); return end
  job.path=event.path; job.waypoint=1; job.phase="walking"
end)

script.on_event(defines.events.on_tick,function()
  if state.session then
    local p=ready_player()
    if not p or p.index~=state.player or p.character.unit_number~=state.character then
      halt("玩家或角色已变化，请重新连接。")
      state.session=nil; state.player=nil
    end
  end
  if state.session and game.tick > state.deadline then
    if state.job then finish(false,"连接已失去响应，AI 动作已停止。") end
    state.session=nil; state.player=nil; state.paused=true; state.cache={}; state.order={}
  end
  local job=state.job
  if not job or state.paused then return end
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
      b.update_selected_entity(job.entity.position)
      b.mining_state={mining=true,position=job.entity.position}
      if b.get_main_inventory().get_item_count(job.product)-job.start_count>=job.expected then finish(true,"采集完成。") end
    elseif job.phase=="crafting" and b.crafting_queue_size==0 then finish(true,"手工制作完成，已启动 "..job.started.." 次配方。")
    elseif job.phase=="waiting" and game.tick>=job.until_tick then finish(true,"等待完成。") end
  end)
  if not ok then finish(false,tostring(err)) end
end)

local allowed_actions={observe=true,recipes=true,inspect=true,move=true,mine=true,craft=true,
  build=true,insert=true,extract=true,set_recipe=true,research=true,wait=true,rotate=true,deconstruct=true}
local allowed_fields={action=true,name=true,x=true,y=true,count=true,seconds=true,direction=true,inventory=true}
local function validate(a)
  if type(a)~="table" or not allowed_actions[a.action] then error("未知游戏动作。") end
  for key,value in pairs(a) do
    if not allowed_fields[key] then error("动作包含未知参数。") end
    if key=="name" or key=="inventory" then
      if type(value)~="string" or #value>100 then error("物品名称无效。") end
    elseif key=="x" or key=="y" or key=="count" or key=="seconds" then
      if type(value)~="number" or value~=value or math.abs(value)==math.huge then error("数值无效。") end
    end
  end
  if a.count and (a.count<1 or a.count>100 or a.count~=math.floor(a.count)) then error("数量无效。") end
  if a.seconds and (a.seconds<0.1 or a.seconds>120) then error("等待时长无效。") end
  if a.x and math.abs(a.x)>100000 or a.y and math.abs(a.y)>100000 then error("目的地超出范围。") end
  if a.direction and not directions[a.direction] then error("方向无效。") end
  if (a.action=="move" or a.action=="build") and (not a.x or not a.y) then error("缺少坐标。") end
  if ({mine=true,craft=true,build=true,insert=true,extract=true,set_recipe=true,research=true})[a.action]
     and (not a.name or a.name=="") then error("缺少名称。") end
end

halt=function(message)
  if state.job then
    -- The character can have died or been detached while an action was running.
    pcall(function() local p=bot(); p.walking_state={walking=false,direction=defines.direction.north}; p.mining_state={mining=false} end)
    state.last_result={ok=false,message=message,action=state.job.a.action,tick=game.tick}
    state.job=nil
  end
  state.paused=true
end

local function call(request)
  local method=request.method
  if method=="hello" then
    local result=connection()
    result.ready=result.connected
    if not result.ready then result.error="请亲自进入单人存档，等待角色就绪后再连接。" end
    return result
  end
  if type(request.session)~="string" or not string.match(request.session,"^[0-9a-f]+$") or #request.session~=32 then
    error("连接身份无效。")
  end
  if method=="claim" then
    if state.session and state.session~=request.session then error("另一个 PlayALL 正在连接这个角色。") end
    local p=ready_player()
    if not p then error("请亲自进入单人地图；多人游戏、无角色或重生界面暂不接管。") end
    for name in pairs(script.active_mods) do
      if not ({base=true,quality=true,["space-age"]=true,["elevated-rails"]=true,["playall-bridge"]=true})[name] then
        error("当前存档启用了尚未验证的第三方模组，请先使用原版或官方扩展。")
      end
    end
    state.session=request.session; state.player=p.index; state.character=p.character.unit_number; state.deadline=game.tick+180
    state.paused=true
    return connection()
  end
  if state.session~=request.session then error("连接已失效或存档已切换，请重新连接。") end
  state.deadline=game.tick+180
  if method=="heartbeat" then return {ok=true} end
  if method=="pause" or method=="cancel" then halt("AI 已暂停，继续时重新观察。"); return {ok=true,replan=true} end
  if method=="release" then
    halt("AI 已断开，角色交还玩家。")
    state.session=nil; state.player=nil
    return {ok=true}
  end
  if method=="resume" then
    local p=bot()
    if not ready_player() then error("游戏角色已变化，请重新连接。") end
    if p.cursor_stack.valid_for_read then error("请先在游戏里放下手持物品，再继续 AI。") end
    state.paused=false
    return {ok=true,replan=true}
  end
  if method=="observe" then return observe() end
  if method=="checkpoint" then
    bot()
    if game.is_multiplayer() then error("不自动保存多人游戏。") end
    if request.value~=1 and request.value~=2 and request.value~=3 then error("保存槽位无效。") end
    game.auto_save("playall-"..request.value)
    return {ok=true,pending=true}
  end
  if method=="execute" then validate(request.value); return dispatch(request.value) end
  error("不支持这个接口；没有新游戏、读档、退出或执行代码的能力。")
end

commands.add_command("playall-bridge","PlayALL 本机连接组件。",function(event)
  -- A player typing this in chat never gets an automation lease.
  if event.player_index~=nil then return end
  local parsed,request=pcall(helpers.json_to_table,event.parameter or "")
  if not parsed or type(request)~="table" or request.protocol~=2 or type(request.id)~="string"
     or not string.match(request.id,"^[0-9a-f]+$") or #request.id~=32 then
    rcon.print('PLAYALL:{"protocol":2,"ok":false,"error":"invalid_request"}')
    return
  end
  local key=helpers.table_to_json(request)
  local cached=state.cache[request.id]
  if cached then
    if cached.key==key then rcon.print(cached.reply)
    else rcon.print("PLAYALL:"..helpers.table_to_json{protocol=2,id=request.id,session=request.session,ok=false,error="request_id_reused"}) end
    return
  end
  local ok,result=pcall(call,request)
  if not ok and request.session==state.session then halt("游戏拒绝了指令，AI 已暂停。") end
  local response={protocol=2,id=request.id,session=request.session,ok=ok}
  if ok then response.result=result else response.error=tostring(result) end
  local reply="PLAYALL:"..helpers.table_to_json(response)
  state.cache[request.id]={key=key,reply=reply}; state.order[#state.order+1]=request.id
  if #state.order>128 then state.cache[table.remove(state.order,1)]=nil end
  rcon.print(reply)
end)
