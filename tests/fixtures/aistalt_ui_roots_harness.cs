// Test-owned fakes only. No Unity, FairyGUI, game DLL or BepInEx is linked.
// The Python test inserts the current production methods at the marker below.
using System;
using System.Collections.Generic;
using System.Reflection;
using FairyGUI;
using UnityEngine;
using UnityEngine.SceneManagement;
using InfantGod.Core.SceneManagement;

namespace UnityEngine
{
    public class Object
    {
        public static readonly List<object> Objects = new List<object>();
        public static T[] FindObjectsOfType<T>() where T : class
        {
            List<T> found = new List<T>();
            foreach (object value in Objects) if (value is T) found.Add(value as T);
            return found.ToArray();
        }
        public static T FindObjectOfType<T>() where T : class
        {
            foreach (object value in Objects) if (value is T) return value as T;
            return null;
        }
    }
    public class GameObject { public bool activeInHierarchy = true; }
    public static class Application { public static bool isBatchMode; }
}

namespace UnityEngine.SceneManagement
{
    public struct Scene
    {
        public string name;
        public bool isLoaded;
        public Scene(string name, bool loaded) { this.name = name; isLoaded = loaded; }
    }
    public static class SceneManager
    {
        public static Scene Active;
        public static Scene GetActiveScene() { return Active; }
    }
}

namespace InfantGod.Core.SceneManagement
{
    public class GameSceneManager { public bool IsLoading; }
}

namespace FairyGUI
{
    public class DisplayObject
    {
        public bool visible = true;
        public DisplayObject parent;
    }
    public class Container : DisplayObject { public bool touchable = true; }
    public class GObject
    {
        public string id;
        public bool isDisposed, onStage = true, visible = true;
        public float alpha = 1;
        public GComponent parent;
        public DisplayObject displayObject = new DisplayObject();
        public GObject(string id) { this.id = id; }
    }
    public class GComponent : GObject
    {
        private readonly List<GObject> children = new List<GObject>();
        public readonly HashSet<GObject> Clipped = new HashSet<GObject>();
        public GComponent(string id) : base(id) { }
        public int numChildren { get { return children.Count; } }
        public GObject GetChildAt(int index) { return children[index]; }
        public bool IsChildInView(GObject child) { return !Clipped.Contains(child); }
        public void AddChild(GObject child)
        {
            children.Add(child);
            child.parent = this;
            child.displayObject.parent = displayObject;
        }
    }
    public class GRoot : GComponent
    {
        private static GRoot _inst;
        public static int GetterCalls;
        public GRoot(string id) : base(id) { }
        public static void SetExisting(GRoot value) { _inst = value; }
        public static GRoot inst
        {
            get { GetterCalls++; throw new InvalidOperationException("A probe tried to lazily create the stage root."); }
        }
    }
    public class UIPanel : UnityEngine.Object
    {
        private GComponent _ui;
        public bool enabled = true;
        public GameObject gameObject = new GameObject();
        public Container container = new Container();
        public static int GetterCalls;
        public UIPanel(GComponent existing)
        {
            _ui = existing;
            if (existing != null) existing.displayObject.parent = container;
            Objects.Add(this);
        }
        public GComponent ui
        {
            get { GetterCalls++; throw new InvalidOperationException("A probe tried to lazily create panel UI."); }
        }
    }
}

public sealed class BridgeUnderTest
{
    /* PRODUCTION_MEMBERS */

    public GRoot Existing() { return ExistingStageRoot(); }
    public List<GComponent> Roots() { return UIRoots(); }
    public bool Ready(List<GComponent> roots, out string reason) { return UIReady(roots, out reason); }
    public bool IsVisible(GObject item) { return Visible(item); }
    public bool Interactive(GComponent root) { return rootInteraction[root]; }
}

public static class Program
{
    private static void Assert(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }
    private static GComponent Content(string id)
    {
        GComponent component = new GComponent(id);
        component.AddChild(new GObject(id + "-child"));
        return component;
    }
    private static GRoot ExistingRoot(bool populated)
    {
        GRoot stage = new GRoot("stage");
        if (populated) stage.AddChild(new GObject("existing-stage-child"));
        GRoot.SetExisting(stage);
        return stage;
    }
    private static void Reset()
    {
        UnityEngine.Object.Objects.Clear();
        GRoot.SetExisting(null);
        GRoot.GetterCalls = UIPanel.GetterCalls = 0;
        Application.isBatchMode = false;
        SceneManager.Active = new Scene("MainMenu", true);
    }
    private static void ExpectReady(BridgeUnderTest bridge, List<GComponent> roots, bool expected)
    {
        string reason;
        bool ready = bridge.Ready(roots, out reason);
        Assert(ready == expected, "Unexpected readiness: " + ready + ", reason: " + reason);
        Assert((reason.Length == 0) == expected, "Readiness and reason disagree.");
    }

    private static void PanelBesideEmptyStage()
    {
        GRoot stage = ExistingRoot(false);
        GComponent menu = Content("menu");
        new UIPanel(menu);
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 1 && System.Object.ReferenceEquals(roots[0], menu), "Displayed UIPanel was missed beside empty GRoot.");
        Assert(System.Object.ReferenceEquals(bridge.Existing(), stage), "Existing stage identity was replaced.");
        Assert(stage.numChildren == 0, "Probe changed empty stage children.");
        ExpectReady(bridge, roots, true);
    }
    private static void PanelWithoutStage()
    {
        GComponent menu = Content("menu");
        new UIPanel(menu);
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(bridge.Existing() == null, "Probe created a stage.");
        Assert(roots.Count == 1 && System.Object.ReferenceEquals(roots[0], menu), "Panel requires an unrelated stage root.");
        ExpectReady(bridge, roots, true);
    }
    private static void UncreatedPanel()
    {
        ExistingRoot(false);
        new UIPanel(null);
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 0, "Uncreated panel was accepted.");
        ExpectReady(bridge, roots, false);
    }
    private static void RejectedPanel(string kind)
    {
        GComponent menu = Content("menu");
        UIPanel panel = new UIPanel(menu);
        if (kind == "panel_hidden") menu.visible = false;
        else if (kind == "panel_inactive") panel.gameObject.activeInHierarchy = false;
        else if (kind == "panel_disabled") panel.enabled = false;
        else if (kind == "panel_disposed") menu.isDisposed = true;
        else if (kind == "panel_off_stage") menu.onStage = false;
        else if (kind == "panel_alpha_zero") menu.alpha = 0;
        else throw new Exception("Unknown panel rejection scenario.");
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 0, "Non-visible/non-active panel counted as a root: " + kind);
        ExpectReady(bridge, roots, false);
    }
    private static void EmptyPanel()
    {
        new UIPanel(new GComponent("empty"));
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 0, "Empty panel counted as displayed game UI.");
        ExpectReady(bridge, roots, false);
    }
    private static void DuplicateRoots()
    {
        GRoot stage = ExistingRoot(true);
        new UIPanel(stage); // One component reachable from both enumeration paths.
        GComponent menu = Content("menu");
        new UIPanel(menu);
        new UIPanel(menu); // Two native panels referencing the same component.
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 2, "Duplicate roots were walked more than once.");
        Assert(roots.Contains(stage) && roots.Contains(menu), "Deduplication lost a distinct root.");
        Assert(roots[0].id == "menu" && roots[1].id == "stage", "Root order was not deterministic.");
    }
    private static void StageChildrenPreserved()
    {
        GRoot stage = ExistingRoot(true);
        GObject first = stage.GetChildAt(0);
        GObject second = new GObject("second-child");
        stage.AddChild(second);
        GComponent menu = Content("menu");
        new UIPanel(menu);
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 2 && roots.Contains(stage) && roots.Contains(menu), "Existing GRoot children were ignored.");
        Assert(stage.numChildren == 2 && System.Object.ReferenceEquals(stage.GetChildAt(0), first)
               && System.Object.ReferenceEquals(stage.GetChildAt(1), second), "Probe rewrote or reparented stage children.");
        Assert(System.Object.ReferenceEquals(first.parent, stage) && System.Object.ReferenceEquals(second.parent, stage), "Child parent identity changed.");
        ExpectReady(bridge, roots, true);
    }
    private static void ReadinessGuard(string kind)
    {
        new UIPanel(Content("menu"));
        if (kind == "batch") Application.isBatchMode = true;
        else if (kind == "bootstrap") SceneManager.Active = new Scene("Bootstrap", true);
        else if (kind == "unloaded_scene") SceneManager.Active = new Scene("MainMenu", false);
        else if (kind == "loading_manager") UnityEngine.Object.Objects.Add(new GameSceneManager { IsLoading = true });
        else if (kind == "loading_scene") SceneManager.Active = new Scene("LoadingScene", true);
        else throw new Exception("Unknown readiness guard scenario.");
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 1, "Readiness guard test did not contain displayed UI.");
        ExpectReady(bridge, roots, false);
    }
    private static void MainGameReady()
    {
        SceneManager.Active = new Scene("MainGame", true);
        UnityEngine.Object.Objects.Add(new GameSceneManager { IsLoading = false });
        new UIPanel(Content("game"));
        BridgeUnderTest bridge = new BridgeUnderTest();
        ExpectReady(bridge, bridge.Roots(), true);
    }
    private static void VisibleAncestors(string kind)
    {
        GComponent parent = new GComponent("parent");
        GComponent menu = Content("menu");
        parent.AddChild(menu);
        UIPanel panel = new UIPanel(menu);
        if (kind == "hidden_gobject_ancestor") parent.visible = false;
        else if (kind == "transparent_gobject_ancestor") parent.alpha = 0;
        else if (kind == "clipped_by_gobject_parent") parent.Clipped.Add(menu);
        else if (kind == "hidden_display_ancestor") panel.container.parent = new DisplayObject { visible = false };
        else if (kind == "hidden_display_object") menu.displayObject.visible = false;
        else if (kind == "missing_display_object") menu.displayObject = null;
        else throw new Exception("Unknown visibility guard scenario.");
        BridgeUnderTest bridge = new BridgeUnderTest();
        Assert(!bridge.IsVisible(menu), "Hidden/clipped display ancestry was accepted: " + kind);
        Assert(bridge.Roots().Count == 0, "Hidden ancestry panel was retained as a game root.");
    }
    private static void NoninteractivePanelCanBeObserved()
    {
        GComponent menu = Content("menu");
        UIPanel panel = new UIPanel(menu);
        panel.container.touchable = false;
        BridgeUnderTest bridge = new BridgeUnderTest();
        List<GComponent> roots = bridge.Roots();
        Assert(roots.Count == 1 && roots.Contains(menu), "Readable noninteractive panel was discarded.");
        Assert(!bridge.Interactive(menu), "Native panel interaction guard was lost.");
    }

    public static int Main(string[] args)
    {
        string scenario = args.Length == 1 ? args[0] : "";
        try
        {
            Reset();
            if (scenario == "panel_beside_empty_stage") PanelBesideEmptyStage();
            else if (scenario == "panel_without_stage") PanelWithoutStage();
            else if (scenario == "uncreated_panel") UncreatedPanel();
            else if (scenario.StartsWith("panel_") && scenario != "panel_empty") RejectedPanel(scenario);
            else if (scenario == "panel_empty") EmptyPanel();
            else if (scenario == "duplicate_roots") DuplicateRoots();
            else if (scenario == "stage_children_preserved") StageChildrenPreserved();
            else if (scenario == "batch" || scenario == "bootstrap" || scenario == "unloaded_scene"
                     || scenario == "loading_manager" || scenario == "loading_scene") ReadinessGuard(scenario);
            else if (scenario == "main_game_ready") MainGameReady();
            else if (scenario == "hidden_gobject_ancestor" || scenario == "transparent_gobject_ancestor"
                     || scenario == "clipped_by_gobject_parent" || scenario == "hidden_display_ancestor"
                     || scenario == "hidden_display_object" || scenario == "missing_display_object") VisibleAncestors(scenario);
            else if (scenario == "noninteractive_panel") NoninteractivePanelCanBeObserved();
            else throw new Exception("Unknown scenario: " + scenario);
            Assert(UIPanel.GetterCalls == 0, "Panel ui lazy getter was touched.");
            Assert(GRoot.GetterCalls == 0, "Stage inst lazy getter was touched.");
            Console.WriteLine("PASS " + scenario);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("FAIL " + scenario + ": " + error.ToString());
            return 1;
        }
    }
}
