// Test-owned UI/JSON fakes; production permission methods are inserted verbatim.
using System;
using System.Collections.Generic;
using System.Text;
using FairyGUI;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace UnityEngine { public static class PlayerPrefs { public static int GetInt(string key, int fallback) { return fallback; } } }

namespace Newtonsoft.Json { public enum Formatting { None } }
namespace Newtonsoft.Json.Linq
{
    public class JObject : SortedDictionary<string, object>
    {
        public new object this[string key] { get { object value; TryGetValue(key, out value); return value; } set { base[key] = value; } }
        public object DeepClone() { JObject result = new JObject(); foreach (var pair in this) result[pair.Key] = pair.Value; return result; }
        public string ToString(Formatting format) { StringBuilder value = new StringBuilder(); foreach (var pair in this) value.Append(pair.Key).Append(':').Append(pair.Value).Append('|'); return value.ToString(); }
    }
}
namespace FairyGUI
{
    public class Controller { public string selectedPage; }
    public class GObject
    {
        public string name = "", text = "", resourceURL = "";
        public GComponent parent;
        public bool usable = true;
    }
    public class GComponent : GObject
    {
        private List<GObject> children = new List<GObject>();
        public Dictionary<string, Controller> controllers = new Dictionary<string, Controller>();
        public int numChildren { get { return children.Count; } }
        public GObject GetChildAt(int index) { return children[index]; }
        public int GetChildIndex(GObject child) { return children.IndexOf(child); }
        public GObject GetChild(string name) { return children.Find(x => x.name == name); }
        public Controller GetController(string name) { Controller value; controllers.TryGetValue(name, out value); return value; }
        public GObject Add(GObject value) { children.Add(value); value.parent = this; return value; }
    }
    public class GList : GComponent { }
    public class GButton : GComponent { }
}
namespace MainMenu
{
    public class UI_MainMenu : GComponent { }
    public class UI_SaveSlot_NewGame : GButton { }
    public class UI_SaveSlot : GButton { }
}
namespace OS { public class UI_Return : GComponent { } }

public sealed class BridgeUnderTest
{
    private Dictionary<string, GObject> controls = new Dictionary<string, GObject>();
    private string viewKey = "screen-a", owner = "human-app", ownerSession = "session-a";
    private int revision = 1;
    /* PRODUCTION_MEMBERS */
    private JObject Observe() { return new JObject(); }
    private bool Usable(GObject item) { return item != null && item.usable; }
    private static JObject Obj(params object[] values) { JObject result = new JObject(); for (int n=0;n<values.Length;n+=2) result[(string)values[n]]=values[n+1]; return result; }
    public string Classify(GObject value) { return ImportantOperation(value); }
    public void SetTarget(string id, GObject target) { controls[id] = target; }
    public void ChangeScreen() { viewKey = "screen-b"; revision++; }
    public void PauseResume() { revision += 2; }
    public void ChangeOwner() { owner = "another-app"; }
    public JObject Preflight(JObject action) { return ReviewAction(action, owner, ownerSession); }
    public JObject Approve(string ticket, string controller, string session) { return ApproveAction(ticket, controller, session); }
    public bool Consume(JObject action, GObject target, string grant) { return ConsumeApproval(action, target, grant); }
}

public static class Program
{
    private static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static GObject Button(string name) { return new GButton { name = name }; }
    private static JObject Action(string id, int revision) { return new JObject { {"action","click"}, {"control_id",id}, {"view_revision",revision} }; }
    public static int Main(string[] args)
    {
        try
        {
            string test = args[0];
            var bridge = new BridgeUnderTest();
            var root = new MainMenu.UI_MainMenu();
            var target = root.Add(Button("Btn_NewGame"));
            if (test == "classify_new_load")
            {
                Assert(bridge.Classify(target)=="new_game_menu", "new menu");
                Assert(bridge.Classify(root.Add(Button("Btn_LoadGame")))=="load_game_menu", "load menu");
                var list = new GList { name="List_SaveSlot_NewGame" }; root.Add(list);
                Assert(bridge.Classify(list.Add(new MainMenu.UI_SaveSlot_NewGame()))=="new_game", "empty slot commits");
                var loadList = new GList { name="List_SaveSlot" }; root.Add(loadList);
                var slot = new MainMenu.UI_SaveSlot(); loadList.Add(slot);
                Assert(bridge.Classify(slot.Add(Button("Btn_Load")))=="load_game", "actual load button");
                Assert(bridge.Classify(slot.Add(Button("LoadDrop")))==null, "dropdown just selects");
                Assert(bridge.Classify(slot)==null, "slot background has no load handler");
            }
            else if (test == "classify_yes_precise")
            {
                var yes = root.Add(Button("Btn_Yes"));
                root.controllers["Warning"] = new Controller { selectedPage="no" };
                Assert(bridge.Classify(yes)==null, "hidden warning");
                root.controllers["Warning"].selectedPage="yes";
                Assert(bridge.Classify(yes)=="overwrite_save", "direct overwrite yes");
                var reminder = new GComponent(); root.Add(reminder);
                Assert(bridge.Classify(reminder.Add(Button("Btn_Yes")))==null, "first reminder yes harmless");
                Assert(bridge.Classify(root.Add(Button("Btn_Confirm")))==null, "unrelated confirm harmless");
            }
            else if (test == "classify_taskbar_return")
            {
                var menu = new GComponent { name="TaskBar_Menu" };
                var list = new GList { name="Menu_List" }; menu.Add(list);
                list.Add(Button("settings")); list.Add(Button("other"));
                Assert(bridge.Classify(list.Add(Button("save")))==null, "save is throttled separately");
                Assert(bridge.Classify(list.Add(Button("load")))=="quick_load", "native quickload row");
                Assert(bridge.Classify(list.Add(new GButton { resourceURL="ui://b4ubvdvurwwt2l" }))=="return_to_menu", "actual exit url");
                Assert(bridge.Classify(menu.Add(Button("Btn_ReturntoHub")))==null, "settings back harmless");
                var returns = new OS.UI_Return();
                Assert(bridge.Classify(returns.Add(Button("Btn_Confirm")))=="return_to_menu", "world return commit");
                Assert(bridge.Classify(returns.Add(Button("Btn_Cancel")))==null, "cancel harmless");
            }
            else
            {
                bridge.SetTarget("target",target);
                JObject action = Action("target",1);
                JObject request = bridge.Preflight(action);
                Assert((bool)request["requires_confirmation"], "preflight must ask");
                Assert(!bridge.Consume(action,target,(string)request["ticket"]), "review ticket is not approval");
                if (test=="stale_ui")
                {
                    bridge.ChangeScreen();
                    Assert(!(bool)bridge.Approve((string)request["ticket"],"human-app","session-a")["ok"], "manual UI change invalidates");
                }
                else if (test=="wrong_session")
                {
                    Assert(!(bool)bridge.Approve((string)request["ticket"],"human-app","other-session")["ok"], "cannot cross transport sessions");
                }
                else
                {
                    JObject allowed=bridge.Approve((string)request["ticket"],"human-app","session-a");
                    string grant=(string)allowed["approval"];
                    Assert((bool)allowed["ok"] && grant!= (string)request["ticket"], "explicit approval issues fresh grant");
                    if (test=="changed_action")
                    {
                        Assert(!bridge.Consume(Action("another-target",1),target,grant), "action binding");
                        Assert(!bridge.Consume(action,new GButton(),grant), "object binding");
                        Assert(!bridge.Consume(action,target,"forged"), "unforgeable grant");
                    }
                    else if (test=="changed_owner")
                    {
                        bridge.ChangeOwner();
                        Assert(!bridge.Consume(action,target,grant), "owner binding");
                    }
                    else if (test=="one_use")
                    {
                        bridge.PauseResume();
                        Assert(bridge.Consume(Action("target",3),target,grant), "pause/resume does not change UI identity");
                        Assert(!bridge.Consume(Action("target",3),target,grant), "grant can only be consumed once");
                    }
                    else if (test=="stale_after_approval")
                    {
                        bridge.ChangeScreen();
                        Assert(!bridge.Consume(action,target,grant), "UI changed after consent");
                    }
                    else throw new Exception("unknown scenario");
                }
            }
            Console.WriteLine("PASS " + test);
            return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
