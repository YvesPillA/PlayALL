-- Test-owned synthetic world only. The helpers above this file are extracted
-- byte-for-byte from the production bridge. Fixture setup is never installed
-- into the user's game and is not evidence of normal player progression.
commands.add_command("playall-native-fixture", "Isolated native API checks", function(event)
  if event.player_index then return end
  local checks, entities = {}, {}
  local function check(name, run)
    local ok, result = pcall(run)
    checks[#checks+1]={name=name,status=ok and "passed" or "failed",detail=ok and result or tostring(result)}
  end
  local surface=game.surfaces[1]
  surface.request_to_generate_chunks({0,0},2)
  surface.force_generate_chunk_requests()
  local tiles={}
  for x=-50,50 do for y=-15,15 do tiles[#tiles+1]={name="grass-1",position={x,y}} end end
  surface.set_tiles(tiles)
  for _, e in pairs(surface.find_entities_filtered{area={{-50,-15},{50,15}},type={"tree","resource","simple-entity"}}) do e.destroy() end
  local serial=0
  local function entity(name)
    if entities[name] then return entities[name] end
    serial=serial+1
    local e=surface.create_entity{name=name,position={-45+(serial%16)*6,math.floor(serial/16)*6-8},force="player"}
    assert(e,"cannot create fixture entity "..name)
    entities[name]=e
    return e
  end
  check("container_has_no_fuel_or_machine_input",function()
    local e=entity("wooden-chest")
    e.insert{name="firearm-magazine",count=8}
    local info=machine_info(e)
    assert(info.contents["firearm-magazine"]==8)
    assert(next(info.fuel)==nil and next(info.input)==nil and next(info.output)==nil,helpers.table_to_json(info))
    assert(inventory_for(e,"lab")==nil and inventory_for(e,"ammo")==nil)
    return info
  end)
  check("furnace_inventories_are_distinct",function()
    local e=entity("stone-furnace")
    inventory_for(e,"fuel").insert{name="coal",count=5}
    inventory_for(e,"input").insert{name="iron-ore",count=7}
    inventory_for(e,"output").insert{name="iron-plate",count=3}
    local info=machine_info(e)
    assert(info.fuel.coal==5 and info.input["iron-ore"]==7 and info.output["iron-plate"]==3)
    assert(next(info.contents)==nil and inventory_for(e,"ammo")==nil)
    return info
  end)
  check("assembler_input_output_modules",function()
    local e=entity("assembling-machine-2")
    e.set_recipe("iron-gear-wheel")
    assert(inventory_for(e,"input").insert{name="iron-plate",count=4}==4)
    assert(inventory_for(e,"output").insert{name="iron-gear-wheel",count=2}==2)
    assert(inventory_for(e,"modules").insert{name="speed-module",count=1}==1)
    local info=machine_info(e)
    assert(info.input["iron-plate"]==4 and info.output["iron-gear-wheel"]==2 and info.modules["speed-module"]==1)
    assert(next(info.fuel)==nil and next(info.contents)==nil)
    return info
  end)
  check("lab_and_turret_inventories",function()
    local lab=entity("lab")
    assert(inventory_for(lab,"lab").insert{name="automation-science-pack",count=4}==4)
    assert(inventory_for(lab,"modules").insert{name="speed-module",count=1}==1)
    local turret=entity("gun-turret")
    assert(inventory_for(turret,"ammo").insert{name="firearm-magazine",count=6}==6)
    local a,b=machine_info(lab),machine_info(turret)
    assert(a.lab["automation-science-pack"]==4 and a.modules["speed-module"]==1)
    assert(b.ammo["firearm-magazine"]==6 and next(b.fuel)==nil and next(a.input)==nil)
    return {lab=a,turret=b}
  end)
  check("burner_inserter_and_drill_geometry",function()
    local a,b=entity("burner-inserter"),entity("burner-mining-drill")
    assert(inventory_for(a,"fuel").insert{name="coal",count=2}==2)
    assert(inventory_for(b,"fuel").insert{name="coal",count=3}==3)
    local ai,bi=machine_info(a),machine_info(b)
    assert(ai.pickup_position and ai.drop_position and bi.drop_position)
    assert(ai.fuel.coal==2 and bi.fuel.coal==3 and next(ai.contents)==nil and next(bi.output)==nil)
    return {inserter=ai,drill=bi}
  end)
  check("belt_line_contents",function()
    local e=entity("transport-belt")
    assert(e.get_transport_line(1).insert_at_back{name="iron-ore",count=1})
    assert(e.get_transport_line(2).insert_at_back{name="coal",count=1})
    local info=machine_info(e)
    assert(info.belt_lines[1]["iron-ore"]==1 and info.belt_lines[2].coal==1)
    return info
  end)
  check("fluids_and_electric_network_are_readable",function()
    local boiler=entity("boiler")
    boiler.fluidbox[1]={name="water",amount=100,temperature=15}
    local info=machine_info(boiler)
    assert(#info.fluids==1 and info.fluids[1].name=="water" and info.fluids[1].amount==100)
    machine_info(entity("steam-engine"))
    machine_info(entity("small-electric-pole"))
    return info
  end)
  check("remaining_inventory_types_and_entity_properties",function()
    local out={}
    for _, name in ipairs{"electric-furnace","beacon","car","spidertron","linked-chest","logistic-chest-passive-provider",
                         "rocket-silo","offshore-pump","pipe","accumulator","solar-panel","radar","artillery-turret"} do
      if prototypes.entity[name] then out[#out+1]=machine_info(entity(name)) end
    end
    if prototypes.entity["agricultural-tower"] then out[#out+1]=machine_info(entity("agricultural-tower")) end
    return {count=#out}
  end)
  check("technology_trigger_metadata",function()
    local tech=technology_info(game.forces.player.technologies["steam-power"])
    assert(tech.trigger and tech.trigger.type=="craft-item" and tech.trigger.item.name=="iron-plate" and tech.trigger.count==50,helpers.table_to_json(tech))
    local normal=technology_info(game.forces.player.technologies.automation)
    assert(not normal.trigger and normal.count==10 and #normal.ingredients==1,helpers.table_to_json(normal))
    return {trigger=tech,normal=normal}
  end)
  check("recipe_hidden_metadata",function()
    local recipe=recipe_info(game.forces.player.recipes["iron-gear-wheel"])
    assert(recipe.hidden==false and recipe.enabled and recipe.ingredients[1].name=="iron-plate")
    return recipe
  end)
  check("build_confirmation_accepts_native_cursor_refill",function()
    local inv,cursor=game.create_inventory(1),game.create_inventory(1)
    inv.insert{name="transport-belt",count=2};cursor.insert{name="transport-belt",count=1}
    local belt=entity("transport-belt")
    local p={cursor_stack=cursor[1],surface=surface,force=game.forces.player}
    local before=carried_count(p,inv,"transport-belt")
    assert(before==3)
    inv.remove{name="transport-belt",count=1}
    assert(cursor[1].valid_for_read and build_confirmed(p,inv,"transport-belt","transport-belt",belt.position,before))
    inv.destroy();cursor.destroy()
    return {scope="native inventory/cursor/entity boundary; consumption precondition supplied by fixture",before=3,after=2,cursor_still_holds_next_item=true}
  end)
  check("build_confirmation_rejects_unconsumed_item",function()
    local inv,cursor=game.create_inventory(1),game.create_inventory(1)
    inv.insert{name="transport-belt",count=2};cursor.insert{name="transport-belt",count=1}
    local p={cursor_stack=cursor[1],surface=surface,force=game.forces.player}
    assert(not build_confirmed(p,inv,"transport-belt","transport-belt",entity("transport-belt").position,3))
    inv.destroy();cursor.destroy()
    return {existing_entity_alone_is_not_success=true}
  end)
  check("build_confirmation_rejects_missing_entity",function()
    local inv,cursor=game.create_inventory(1),game.create_inventory(1)
    inv.insert{name="transport-belt",count=1}
    local p={cursor_stack=cursor[1],surface=surface,force=game.forces.player}
    assert(not build_confirmed(p,inv,"transport-belt","transport-belt",{2000,2000},2))
    inv.destroy();cursor.destroy()
    return {consumption_without_entity_is_not_success=true}
  end)
  local function transfer_fixture(capacity_override)
    local source,dest=game.create_inventory(1),game.create_inventory(1)
    source.insert{name="iron-plate",count=20}
    dest.insert{name="iron-plate",count=99}
    local target=dest
    if capacity_override then target={get_insertable_count=function() return 100 end,insert=function(stack) return dest.insert(stack) end} end
    local player={get_main_inventory=function() return target end,surface=surface,position={0,0},force=game.forces.player}
    return source,dest,player
  end
  check("partial_capacity_extract_preserves_total",function()
    local src,dst,p=transfer_fixture(false)
    local before=src.get_item_count("iron-plate")+dst.get_item_count("iron-plate")
    local inserted,dropped=extract_items(p,src,"iron-plate",20,true)
    assert(inserted==1 and dropped==0 and src.get_item_count("iron-plate")==19 and dst.get_item_count("iron-plate")==100)
    assert(before==src.get_item_count("iron-plate")+dst.get_item_count("iron-plate"))
    src.destroy();dst.destroy()
    return {inserted=inserted,total=before}
  end)
  check("full_capacity_extract_leaves_source_unchanged",function()
    local src,dst,p=transfer_fixture(false)
    dst.insert{name="iron-plate",count=1}
    local inserted,dropped=extract_items(p,src,"iron-plate",20,true)
    assert(inserted==0 and dropped==0 and src.get_item_count("iron-plate")==20 and dst.get_item_count("iron-plate")==100)
    src.destroy();dst.destroy()
    return {inserted=0,total=120}
  end)
  check("optimistic_capacity_estimate_compensates_remainder",function()
    local src,dst,p=transfer_fixture(true)
    local inserted,dropped=extract_items(p,src,"iron-plate",20,true)
    assert(inserted==1 and dropped==0 and src.get_item_count("iron-plate")==19 and dst.get_item_count("iron-plate")==100)
    src.destroy();dst.destroy()
    return {inserted=inserted,total=119,capacity_estimate_overridden_for_fault_injection=true}
  end)
  local ok=true
  for _, result in ipairs(checks) do if result.status~="passed" then ok=false end end
  rcon.print("FIXTURE:"..helpers.table_to_json{ok=ok,checks=checks,version=script.active_mods.base,
      dlc=script.active_mods["space-age"]~=nil,scope="synthetic isolated native API fixture; no player progression claim"})
end)
