// PlayALL's original bridge. Game code and assets are not redistributed.
// All Unity/game access happens in Update, through existing game APIs/events.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using BepInEx;
using FairyGUI;
using InfantGod.Core;
using InfantGod.Core.AistaltHPSystem;
using InfantGod.Core.CompuSystem;
using InfantGod.Core.DialogueSystem;
using InfantGod.Core.RewindSystem;
using InfantGod.Core.SaveSystem;
using InfantGod.Core.SceneManagement;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.SceneManagement;
using Yarn.Unity;

namespace PlayALL.Aistalt
{
    [BepInPlugin("playall.aistalt.bridge", "PlayALL Aistalt Bridge", "0.1.5")]
    [BepInProcess("Aistalt.exe")]
    public sealed class Bridge : BaseUnityPlugin
    {
        private sealed class Work
        {
            public JObject Request;
            public JObject Result;
            public string Session;
            public TcpClient Client;
            public bool Finished, Cancelled;
        }

        private sealed class Review
        {
            public string Ticket, Approval, ActionKey, ViewKey, Controller, Session;
            public GObject Target;
        }
        private Review review;
        private DateTime lastManualSave = DateTime.MinValue;

        private readonly Queue<Work> queue = new Queue<Work>();
        private readonly Dictionary<string, GObject> controls = new Dictionary<string, GObject>();
        private readonly Dictionary<string, JObject> completed = new Dictionary<string, JObject>();
        private readonly Queue<string> completedIds = new Queue<string>();
        private readonly HashSet<string> sessions = new HashSet<string>();
        private readonly string token = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
        private readonly string instance = Guid.NewGuid().ToString("N");
        private TcpListener listener;
        private Thread server;
        private volatile bool stopped;
        private bool paused = true;
        private string owner, ownerSession, endpoint, buildHash, viewKey = "", line = "";
        private long heartbeat;
        private int revision;
        private int textCharacters;
        private float nextDialogueBind;
        private DialogueLogicManager dialogue;
        private string lifecyclePath;
        private bool firstUpdateLogged, applicationQuitting;
        private readonly object lifecycleLock = new object();
        private static readonly FieldInfo PanelUI = typeof(UIPanel).GetField("_ui", BindingFlags.Instance | BindingFlags.NonPublic);
        private static readonly FieldInfo StageRoot = typeof(GRoot).GetField("_inst", BindingFlags.Static | BindingFlags.NonPublic);
        private readonly HashSet<GObject> walked = new HashSet<GObject>();
        private readonly Dictionary<GObject, string> objectIds = new Dictionary<GObject, string>();
        private readonly Dictionary<GComponent, bool> rootInteraction = new Dictionary<GComponent, bool>();
        private string lastReadiness = "";

        private static JObject Obj(params object[] pairs)
        {
            JObject result = new JObject();
            for (int i = 0; i < pairs.Length; i += 2)
                result[(string)pairs[i]] = pairs[i + 1] == null ? JValue.CreateNull() :
                    pairs[i + 1] as JToken ?? JToken.FromObject(pairs[i + 1]);
            return result;
        }

        private static string Clip(string value, int size)
        {
            value = value ?? "";
            return value.Length > size ? value.Substring(0, size) : value;
        }

        private void Awake()
        {
            lifecyclePath = Path.Combine(Paths.PluginPath, "PlayALL/lifecycle.log");
            LogLifecycle("Awake");
            try
            {
                InitializeBridge();
                LogLifecycle("Awake.ready");
            }
            catch (Exception error)
            {
                LogLifecycle("Awake.failed:" + error.GetType().Name, true);
                throw;
            }
        }

        private void LogLifecycle(string phase, bool includeStack = false)
        {
            // Diagnostic fields describe this component only; never log auth or requests.
            JObject entry = Obj("utc", DateTime.UtcNow.ToString("o"), "event", phase,
                "instance", instance, "thread", Thread.CurrentThread.ManagedThreadId,
                "quitting", applicationQuitting, "paused", paused);
            try
            {
                Transform root = transform.root;
                entry["frame"] = Time.frameCount;
                entry["active_scene"] = SceneManager.GetActiveScene().name;
                entry["object"] = gameObject.name;
                entry["object_scene"] = gameObject.scene.name;
                entry["root"] = root == null ? "" : root.gameObject.name;
                entry["root_scene"] = root == null ? "" : root.gameObject.scene.name;
                entry["hide_flags"] = gameObject.hideFlags.ToString();
                entry["active"] = gameObject.activeInHierarchy;
                entry["enabled"] = enabled;
            }
            catch (Exception error) { entry["state_error"] = error.GetType().Name; }
            if (includeStack)
                entry["stack"] = Clip(new System.Diagnostics.StackTrace(1, true).ToString(), 6000);
            string record = entry.ToString(Formatting.None);
            try { Logger.LogInfo("[lifecycle] " + record); } catch (Exception) { }
            try
            {
                lock (lifecycleLock)
                {
                    if (lifecyclePath == null)
                        lifecyclePath = Path.Combine(Paths.PluginPath, "PlayALL/lifecycle.log");
                    Directory.CreateDirectory(Path.GetDirectoryName(lifecyclePath));
                    if (File.Exists(lifecyclePath) && new FileInfo(lifecyclePath).Length > 256 * 1024)
                    {
                        string previous = lifecyclePath + ".1";
                        if (File.Exists(previous)) File.Delete(previous);
                        File.Move(lifecyclePath, previous);
                    }
                    File.AppendAllText(lifecyclePath, record + "\n", new UTF8Encoding(false));
                }
            }
            catch (Exception error)
            {
                try { Logger.LogWarning("Lifecycle log could not be written: " + error.GetType().Name); }
                catch (Exception) { }
            }
        }

        private void OnEnable() { LogLifecycle("OnEnable"); }

        private void Start() { LogLifecycle("Start"); }

        private void OnDisable() { LogLifecycle("OnDisable"); }

        private void OnApplicationQuit()
        {
            applicationQuitting = true;
            LogLifecycle("OnApplicationQuit");
        }

        private void InitializeBridge()
        {
            // Lifecycle logs verify that the loader's Manager survives startup.
            Application.runInBackground = true;
            using (SHA256 hash = SHA256.Create())
                buildHash = BitConverter.ToString(hash.ComputeHash(File.ReadAllBytes(
                    Path.Combine(Application.dataPath, "Managed/InfantGod.dll")))).Replace("-", "").ToLowerInvariant();
            endpoint = Path.Combine(Paths.PluginPath, "PlayALL/endpoint.json");
            Directory.CreateDirectory(Path.GetDirectoryName(endpoint));
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start(2);
            JObject info = Obj("protocol", 1, "game", "aistalt", "instance", instance,
                "pid", System.Diagnostics.Process.GetCurrentProcess().Id,
                "port", ((IPEndPoint)listener.LocalEndpoint).Port, "token", token,
                "game_sha256", buildHash, "headless", Application.isBatchMode);
            File.WriteAllText(endpoint + ".tmp", info.ToString(Formatting.None), new UTF8Encoding(false));
            if (File.Exists(endpoint)) File.Delete(endpoint);
            File.Move(endpoint + ".tmp", endpoint);
            server = new Thread(Serve);
            server.IsBackground = true;
            server.Name = "PlayALL bridge transport";
            server.Start();
            Logger.LogInfo("PlayALL bridge ready; AI control starts paused.");
        }

        private void Serve()
        {
            while (!stopped)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    ThreadPool.QueueUserWorkItem(delegate { HandleClient(client); });
                }
                catch (SocketException) { if (stopped) return; }
                catch (ObjectDisposedException) { return; }
            }
        }

        private void HandleClient(TcpClient client)
        {
            string session = Guid.NewGuid().ToString("N");
            lock (sessions) sessions.Add(session);
            try
            {
                using (client)
                using (NetworkStream stream = client.GetStream())
                {
                    client.ReceiveTimeout = 8000;
                    client.SendTimeout = 6000;
                    while (!stopped)
                    {
                        List<byte> bytes = new List<byte>();
                        while (true)
                        {
                            int value = stream.ReadByte();
                            if (value < 0) return;
                            if (value == 10) break;
                            if (bytes.Count >= 65536) throw new IOException("Request too large");
                            bytes.Add((byte)value);
                        }
                        JObject request = JObject.Parse(new UTF8Encoding(false, true).GetString(bytes.ToArray()));
                        JObject result;
                        if (!String.Equals((string)request["token"], token, StringComparison.Ordinal)
                            || (string)request["instance"] != instance)
                            result = Obj("ok", false, "error", "Bridge authentication failed.");
                        else
                        {
                            Work work = new Work();
                            work.Request = request;
                            work.Session = session;
                            work.Client = client;
                            lock (queue)
                            {
                                if (queue.Count >= 16) throw new IOException("Bridge is busy");
                                queue.Enqueue(work);
                            }
                            lock (work)
                            {
                                if (!work.Finished) Monitor.Wait(work, 6000);
                                if (!work.Finished)
                                {
                                    work.Cancelled = true;
                                    throw new IOException("Game did not process the command");
                                }
                                result = work.Result;
                            }
                        }
                        result["id"] = request["id"];
                        result["instance"] = instance;
                        byte[] response = Encoding.UTF8.GetBytes(result.ToString(Formatting.None) + "\n");
                        stream.Write(response, 0, response.Length);
                    }
                }
            }
            catch (Exception) { /* Transport failure never retries a game action. */ }
            finally { lock (sessions) sessions.Remove(session); }
        }

        private bool SessionAlive(string session)
        {
            lock (sessions) return session != null && sessions.Contains(session);
        }

        private void Update()
        {
            if (!firstUpdateLogged)
            {
                firstUpdateLogged = true;
                LogLifecycle("Update.first");
            }
            BindDialogue();
            if (owner != null && (!SessionAlive(ownerSession) || DateTime.UtcNow.Ticks - heartbeat > TimeSpan.FromSeconds(6).Ticks))
            {
                paused = true;
                owner = ownerSession = null;
                review = null;
                revision++;
            }
            for (int n = 0; n < 4; n++)
            {
                Work work;
                lock (queue)
                {
                    if (queue.Count == 0) return;
                    work = queue.Dequeue();
                }
                lock (work)
                {
                    if (work.Cancelled) continue;
                    try
                    {
                        if (!SessionAlive(work.Session) || (work.Client.Client.Poll(0, SelectMode.SelectRead) && work.Client.Available == 0))
                            throw new IOException("The controller disconnected before dispatch.");
                        work.Result = Handle(work.Request, work.Session);
                    }
                    catch (Exception error) { work.Result = Obj("ok", false, "error", Clip(error.Message, 1000)); }
                    work.Finished = true;
                    Monitor.PulseAll(work);
                }
            }
        }

        private void BindDialogue()
        {
            if (Time.realtimeSinceStartup < nextDialogueBind) return;
            nextDialogueBind = Time.realtimeSinceStartup + 0.5f;
            DialogueLogicManager found = UnityEngine.Object.FindObjectOfType<DialogueLogicManager>();
            if (found == dialogue) return;
            if (dialogue != null) dialogue.OnLineStarted -= OnLine;
            dialogue = found;
            line = "";
            if (dialogue != null) dialogue.OnLineStarted += OnLine;
            viewKey = "";
        }

        private void OnLine(LocalizedLine value)
        {
            line = value == null ? "" : Clip(value.TextWithoutCharacterName.Text, 12000);
        }

        private GRoot ExistingStageRoot()
        {
            // Read the existing root, never instantiate a Stage during a probe.
            if (StageRoot == null) throw new InvalidOperationException("FairyGUI root metadata does not match this build.");
            return StageRoot.GetValue(null) as GRoot;
        }

        private List<GComponent> UIRoots()
        {
            if (PanelUI == null) throw new InvalidOperationException("FairyGUI panel metadata does not match this build.");
            List<GComponent> roots = new List<GComponent>();
            rootInteraction.Clear();
            GRoot stageRoot = ExistingStageRoot();
            if (Visible(stageRoot) && stageRoot.numChildren > 0)
            {
                roots.Add(stageRoot);
                rootInteraction[stageRoot] = true;
            }
            foreach (UIPanel panel in UnityEngine.Object.FindObjectsOfType<UIPanel>())
            {
                if (!panel.enabled || !panel.gameObject.activeInHierarchy) continue;
                // UIPanel.ui is a lazy getter which can load/create UI. _ui is
                // the game's already-created component in a parallel Stage container.
                GComponent ui = PanelUI.GetValue(panel) as GComponent;
                if (!Visible(ui) || ui.numChildren == 0 || roots.Contains(ui)) continue;
                roots.Add(ui);
                rootInteraction[ui] = panel.container != null && panel.container.touchable;
            }
            roots.Sort(delegate(GComponent left, GComponent right) { return String.CompareOrdinal(left.id, right.id); });
            return roots;
        }

        private bool UIReady(List<GComponent> roots, out string reason)
        {
            Scene scene = SceneManager.GetActiveScene();
            GameSceneManager manager = UnityEngine.Object.FindObjectOfType<GameSceneManager>();
            if (Application.isBatchMode) reason = "无窗口测试模式，AI 不执行游戏动作。";
            else if (!scene.isLoaded || (manager != null && manager.IsLoading)) reason = "游戏正在加载，请稍候。";
            else if (scene.name != "MainMenu" && scene.name != "MainGame") reason = "游戏尚未进入主菜单或游戏场景。";
            else if (roots.Count == 0) reason = "尚未找到游戏已显示的界面面板。";
            else reason = "";
            return reason.Length == 0;
        }

        private bool Visible(GObject item)
        {
            if (item == null || item.isDisposed || !item.onStage) return false;
            for (GObject node = item; node != null; node = node.parent)
            {
                if (!node.visible || node.alpha <= 0 || node.displayObject == null || !node.displayObject.visible) return false;
                if (node.parent != null && !node.parent.IsChildInView(node)) return false;
            }
            for (DisplayObject node = item.displayObject; node != null; node = node.parent)
                if (!node.visible) return false;
            return true;
        }

        private bool Usable(GObject item)
        {
            if (!Visible(item) || !item.enabled || !item.touchable || LifecycleBlocked(item)) return false;
            for (GObject node = item.parent; node != null; node = node.parent)
                if (!node.enabled) return false;
            for (GObject node = item; node != null; node = node.parent)
            {
                bool allowed;
                GComponent component = node as GComponent;
                if (component != null && rootInteraction.TryGetValue(component, out allowed) && !allowed) return false;
            }
            GRoot stageRoot = ExistingStageRoot();
            Window modal = stageRoot == null ? null : stageRoot.GetTopWindow();
            if (modal != null && modal.modal)
            {
                GObject node = item;
                while (node != null && node != modal) node = node.parent;
                if (node == null) return false;
            }
            return true;
        }

        private bool LifecycleBlocked(GObject item)
        {
            for (GObject node = item; node != null; node = node.parent)
            {
                string name = node.name ?? "";
                string kind = node.GetType().Name;
                if (kind == "UI_Btn_Steam" || kind == "UI_Btn_Discord" || kind == "UI_Btn_X" || kind == "UI_Btn_QQ") return true;
                if (name == "Btn_Exit" || name == "Btn_OpenFolder" || name == "Btn_ReturnToHub"
                    || name.IndexOf("Delete", StringComparison.OrdinalIgnoreCase) >= 0) return true;
                if (name == "Panel_GameEnd") return true;
            }
            return false;
        }

        private bool InNamedParent(GObject item, string name)
        {
            for (GObject node = item.parent; node != null; node = node.parent)
                if (node.name == name) return true;
            return false;
        }

        private string ImportantOperation(GObject item)
        {
            // Classify native controls by their real object type and bound context,
            // never by translated game text or a blanket Yes/Confirm/Return match.
            GObject node = item;
            for (; node != null; node = node.parent)
            {
                string type = node.GetType().FullName;
                GComponent parent = node.parent;
                if (parent != null && parent.GetType().FullName == "MainMenu.UI_MainMenu")
                {
                    if (node.name == "Btn_NewGame") return "new_game_menu";
                    if (node.name == "Btn_LoadGame") return "load_game_menu";
                    Controller warning = parent.GetController("Warning");
                    if (node.name == "Btn_Yes" && System.Object.ReferenceEquals(parent.GetChild("Btn_Yes"), node)
                        && warning != null && warning.selectedPage == "yes") return "overwrite_save";
                }
                if (type == "MainMenu.UI_SaveSlot_NewGame" && InNamedParent(node, "List_SaveSlot_NewGame")) return "new_game";
                if (node.name == "Btn_Load" && parent != null && parent.GetType().FullName == "MainMenu.UI_SaveSlot"
                    && InNamedParent(node, "List_SaveSlot")) return "load_game";
                if (node.name == "Btn_Confirm" && parent != null && parent.GetType().FullName == "OS.UI_Return") return "return_to_menu";
                GList list = parent as GList;
                if (list != null && list.name == "Menu_List" && InNamedParent(list, "TaskBar_Menu"))
                {
                    if (node.resourceURL == "ui://b4ubvdvurwwt2l") return "return_to_menu";
                    if (list.numChildren >= 4 && System.Object.ReferenceEquals(list.GetChildAt(3), node)) return "quick_load";
                }
            }
            return null;
        }

        private string ConfirmationQuestion(string operation, GObject target)
        {
            string title = Clip(target.text, 240);
            for (GObject node = target; node != null; node = node.parent)
            {
                string type = node.GetType().FullName;
                if ((type == "MainMenu.UI_SaveSlot" || type == "MainMenu.UI_SaveSlot_NewGame") && node.parent is GList)
                {
                    title = "存档槽 " + (node.parent.GetChildIndex(node) + 1);
                    GComponent slot = node as GComponent;
                    foreach (string name in new string[] { "LoadDrop", "FileLoad_Drop" })
                    {
                        GObject selection = slot == null ? null : slot.GetChild(name);
                        if (selection != null && !String.IsNullOrWhiteSpace(selection.text)) title += "　" + Clip(selection.text, 120);
                    }
                    break;
                }
            }
            if (operation == "overwrite_save") title = "存档槽 " + (PlayerPrefs.GetInt("TempNewGameSlotId", 0) + 1);
            if (String.IsNullOrWhiteSpace(title) && target.parent != null) title = Clip(target.parent.text, 240);
            string suffix = String.IsNullOrWhiteSpace(title) ? "" : "\n当前选择：" + title;
            switch (operation)
            {
                case "new_game_menu": return "要打开新游戏选择界面吗？";
                case "load_game_menu": return "要打开存档选择界面吗？";
                case "new_game": return "要在这个存档槽新建游戏吗？已有内容会先显示游戏的覆盖提示。" + suffix;
                case "overwrite_save": return "这将删除所选槽位的原存档、备份和快速存档，并新建游戏。是否同意本次覆盖？" + suffix;
                case "load_game": return "要进入当前选择的存档吗？当前未保存的进度可能丢失。" + suffix;
                case "quick_load": return "要载入快速存档吗？这会回退当前游戏进度，未保存的进度可能丢失。";
                default: return "要返回主菜单吗？这个操作不会自动保存当前进度。";
            }
        }

        private string ActionKey(JObject action)
        {
            JObject normalized = (JObject)action.DeepClone();
            normalized.Remove("view_revision");
            return normalized.ToString(Formatting.None);
        }

        private JObject ReviewAction(JObject action, string controller, string session)
        {
            Observe();
            if ((string)action["action"] != "click") return Obj("ok", true, "requires_confirmation", false);
            GObject target;
            if ((int?)action["view_revision"] != revision || !controls.TryGetValue((string)action["control_id"] ?? "", out target) || !Usable(target))
                return Obj("ok", false, "error", "游戏界面已变化，请重新观察。");
            string operation = ImportantOperation(target);
            if (operation == null) return Obj("ok", true, "requires_confirmation", false);
            review = new Review { Ticket = Guid.NewGuid().ToString("N"), ActionKey = ActionKey(action),
                ViewKey = viewKey, Target = target, Controller = controller, Session = session };
            return Obj("ok", true, "requires_confirmation", true, "ticket", review.Ticket,
                "question", ConfirmationQuestion(operation, target), "operation", operation);
        }

        private JObject ApproveAction(string ticket, string controller, string session)
        {
            Observe();
            if (review == null || review.Ticket != ticket || review.Controller != controller || review.Session != session
                || review.ViewKey != viewKey || !Usable(review.Target))
            {
                review = null;
                return Obj("ok", false, "reason", "stale_confirmation", "error", "游戏界面已变化，本次确认已失效。");
            }
            review.Approval = Guid.NewGuid().ToString("N");
            return Obj("ok", true, "approval", review.Approval);
        }

        private bool ConsumeApproval(JObject action, GObject target, string approval)
        {
            if (review == null || String.IsNullOrEmpty(approval) || review.Approval != approval
                || review.Controller != owner || review.Session != ownerSession || review.Target != target
                || review.ActionKey != ActionKey(action) || review.ViewKey != viewKey) return false;
            review = null;
            return true;
        }

        private string ControlID(GObject item)
        {
            // Package-authored ids repeat inside reused components. A token is
            // bound to this object instance, rather than a serialized template id.
            string id;
            if (!objectIds.TryGetValue(item, out id))
            {
                id = "ui_" + Guid.NewGuid().ToString("N");
                objectIds[item] = id;
            }
            return id;
        }

        private void Walk(GObject item, string path, JArray actions, JArray text, StringBuilder key, int depth)
        {
            if (depth > 24 || !Visible(item) || !walked.Add(item)) return;
            string objectId = ControlID(item);
            path = path + "/" + (String.IsNullOrEmpty(item.name) ? item.id : item.name);
            GTextInput input = item as GTextInput;
            string content = input != null && input.displayAsPassword ? "[隐藏]" : Clip(item.text, 1600);
            bool clickable = item is GButton || !item.onClick.isEmpty ||
                (item.displayObject != null && !item.displayObject.onClick.isEmpty);
            GComponent component = item as GComponent;
            bool scrollable = component != null && component.scrollPane != null;
            if (clickable || input != null || scrollable || item is GSlider || item is GComboBox)
            {
                bool usable = Usable(item);
                controls[objectId] = item;
                JObject control = Obj("id", objectId, "name", item.name, "path", path,
                    "kind", item.GetType().Name, "text", Clip(content, 512), "enabled", usable,
                    "game_enabled", item.enabled, "ai_allowed", !LifecycleBlocked(item),
                    "click", clickable, "scroll", scrollable,
                    "input", input != null && input.editable && !input.displayAsPassword);
                string operation = ImportantOperation(item);
                if (operation != null)
                {
                    control["requires_confirmation"] = operation;
                    if (operation == "overwrite_save") control["confirmation_slot"] = PlayerPrefs.GetInt("TempNewGameSlotId", 0);
                }
                GComboBox combo = item as GComboBox;
                if (combo != null) control["selected_index"] = combo.selectedIndex;
                GButton button = item as GButton;
                if (button != null) control["selected"] = button.selected;
                GSlider slider = item as GSlider;
                if (slider != null)
                {
                    control["value"] = slider.value;
                    control["min"] = slider.min;
                    control["max"] = slider.max;
                }
                if (scrollable)
                {
                    control["scroll_x"] = component.scrollPane.percX;
                    control["scroll_y"] = component.scrollPane.percY;
                }
                key.Append(control.ToString(Formatting.None)).Append('\n');
                if (actions.Count < 220) actions.Add(control);
            }
            if (!(item is GTextInput) && !String.IsNullOrWhiteSpace(content) && text.Count < 160 && textCharacters < 16000)
            {
                string fragment = Clip(content, 16000 - textCharacters);
                text.Add(Obj("id", objectId, "path", path, "text", fragment));
                textCharacters += fragment.Length;
            }
            if (component != null)
                for (int i = 0; i < component.numChildren; i++)
                    Walk(component.GetChildAt(i), path, actions, text, key, depth + 1);
        }

        private JObject Observe()
        {
            controls.Clear();
            walked.Clear();
            textCharacters = 0;
            JArray actions = new JArray(), text = new JArray();
            StringBuilder key = new StringBuilder();
            Scene scene = SceneManager.GetActiveScene();
            key.Append(scene.handle).Append('|').Append(dialogue == null ? "" : dialogue.CurrentLineID)
                .Append('|').Append(dialogue == null ? 0 : dialogue.CurrentOptionRequestVersion).Append('\n');
            List<GComponent> roots = UIRoots();
            string readyReason;
            bool ready = UIReady(roots, out readyReason);
            foreach (GComponent root in roots)
            {
                if (root is GRoot)
                    for (int i = 0; i < root.numChildren; i++) Walk(root.GetChildAt(i), "stage", actions, text, key, 0);
                else Walk(root, "panel", actions, text, key, 0);
            }
            foreach (GObject item in new List<GObject>(objectIds.Keys))
                if (!walked.Contains(item)) objectIds.Remove(item);
            string current = key.ToString();
            if (current != viewKey) { viewKey = current; revision++; }
            JObject stats = new JObject();
            GameTimeManager clock = UnityEngine.Object.FindObjectOfType<GameTimeManager>();
            AistaltHPManager hp = UnityEngine.Object.FindObjectOfType<AistaltHPManager>();
            CompuSystem compu = UnityEngine.Object.FindObjectOfType<CompuSystem>();
            if (clock != null) { stats["day"] = clock.GetCurrentDay(); stats["time"] = clock.GetTimeString(); }
            if (hp != null) { stats["hp"] = hp.CurrentHp; stats["max_hp"] = hp.MaxHp; }
            if (compu != null) { stats["compu"] = compu.GetCompu(); stats["total_compu"] = compu.GetTotalCompu(); }
            return Obj("ok", true, "game", "aistalt", "source", "真实幼神 Demo", "scene", scene.name,
                "window_connected", ready, "ready_reason", readyReason, "ui_root_count", roots.Count,
                "paused", paused, "manual_control", paused, "view_revision", revision,
                "dialogue", Obj("line_id", dialogue == null ? "" : dialogue.CurrentLineID,
                    "speaker", dialogue == null ? "" : dialogue.CurrentCharacterName,
                    "node", dialogue == null ? "" : dialogue.CurrentNode, "text", line,
                    "state", dialogue == null ? "none" : dialogue.CurrentState.ToString()),
                "stats", stats, "controls", actions, "controls_truncated", controls.Count > actions.Count,
                "visible_text", text, "text_limit_reached", textCharacters >= 16000);
        }

        private JObject Handle(JObject request, string session)
        {
            string controller = (string)request["controller"], method = (string)request["method"];
            if (String.IsNullOrEmpty(controller)) return Obj("ok", false, "error", "Missing controller identity.");
            if (owner != null && (owner != controller || ownerSession != session)) return Obj("ok", false, "error", "另一个 PlayALL 已连接这份游戏。");
            heartbeat = DateTime.UtcNow.Ticks;
            if (method == "hello" || method == "ping")
            {
                List<GComponent> roots = UIRoots();
                string reason;
                bool ready = UIReady(roots, out reason);
                string scene = SceneManager.GetActiveScene().name;
                string state = scene + "|" + ready + "|" + roots.Count;
                if (state != lastReadiness)
                {
                    lastReadiness = state;
                    Logger.LogInfo("[readiness] " + state + " " + reason);
                }
                return Obj("ok", true, "protocol", 1, "game", "aistalt", "game_sha256", buildHash,
                    "ready", ready, "paused", paused, "scene", scene, "ui_root_count", roots.Count, "ready_reason", reason,
                    "loading", reason == "游戏正在加载，请稍候。", "confirmations", true);
            }
            if (method == "observe") return Observe();
            if (method == "pause" || method == "detach")
            {
                paused = true;
                revision++;
                if (method == "detach") { owner = ownerSession = null; review = null; }
                return Obj("ok", true, "replan", true, "manual_control", true);
            }
            if (method == "resume")
            {
                if (review != null && review.Approval == null)
                    return Obj("ok", false, "reason", "awaiting_user", "error", "请先回复当前重要操作；继续不会代替玩家确认。");
                owner = controller;
                ownerSession = session;
                paused = false;
                revision++;
                return Obj("ok", true, "replan", true);
            }
            if (method == "checkpoint") return Checkpoint();
            if (method == "review" || method == "approve" || method == "reject")
            {
                if (owner != controller || ownerSession != session) return Obj("ok", false, "error", "请先连接并开始游玩。");
                if (method == "reject")
                {
                    if (review != null && review.Ticket != (string)request["ticket"])
                        return Obj("ok", false, "error", "这个确认已经失效。");
                    review = null;
                    return Obj("ok", true);
                }
                return method == "review" ? ReviewAction((JObject)request["action"], controller, session) :
                    ApproveAction((string)request["ticket"], controller, session);
            }
            if (method != "execute") return Obj("ok", false, "error", "Unknown bridge method.");
            string windowReason;
            if (!UIReady(UIRoots(), out windowReason))
                return Obj("ok", false, "reason", "window_closed", "error", windowReason);
            if (paused || owner != controller || ownerSession != session) return Obj("ok", false, "error", "AI 已暂停，游戏由玩家操作。");
            if (review != null && review.Approval == null)
                return Obj("ok", false, "reason", "awaiting_user", "error", "等待玩家回复期间不提交游戏动作。");
            string actionId = (string)request["id"];
            if (String.IsNullOrEmpty(actionId)) return Obj("ok", false, "error", "Missing action identity.");
            string id = controller + ":" + actionId;
            JObject prior;
            if (completed.TryGetValue(id, out prior)) return (JObject)prior.DeepClone();
            JObject result = Execute((JObject)request["action"], (string)request["approval"]);
            completed[id] = (JObject)result.DeepClone();
            completedIds.Enqueue(id);
            if (completedIds.Count > 128) completed.Remove(completedIds.Dequeue());
            return result;
        }

        private JObject Execute(JObject action, string approval)
        {
            JObject current = Observe();
            string kind = (string)action["action"];
            if (kind == "observe") return current;
            if (kind == "wait") return Obj("ok", true, "message", "等待游戏自身完成加载或动画。");
            if ((int?)action["view_revision"] != revision)
                return Obj("ok", false, "reason", "stale_view", "error", "游戏界面已变化，请根据新的观察重新决定。", "observation", current);
            string target = (string)action["control_id"];
            if (kind == "advance")
            {
                target = null;
                foreach (GObject item in controls.Values)
                    if (item.name == "Btn_NextLine" && Usable(item)) { target = ControlID(item); break; }
            }
            GObject control;
            if (target == null || !controls.TryGetValue(target, out control) || !Usable(control))
                return Obj("ok", false, "error", "该控件已隐藏、禁用或离开当前界面，请重新观察。");
            if (kind == "click" || kind == "advance")
            {
                string operation = ImportantOperation(control);
                if (operation != null || review != null)
                {
                    if (!ConsumeApproval(action, control, approval))
                    {
                        if (review != null && review.Approval != null)
                        {
                            review = null;
                            return Obj("ok", false, "reason", "stale_confirmation", "error", "已确认的操作与当前游戏界面不匹配，未执行。");
                        }
                        JObject required = ReviewAction(action, owner, ownerSession);
                        required["ok"] = false;
                        required["error"] = "请先等待玩家确认本次重要操作。";
                        return required;
                    }
                }
                GList menu = control.parent as GList;
                if (menu != null && menu.name == "Menu_List" && InNamedParent(menu, "TaskBar_Menu")
                    && menu.numChildren >= 3 && System.Object.ReferenceEquals(menu.GetChildAt(2), control))
                {
                    if ((DateTime.UtcNow - lastManualSave).TotalSeconds < 180)
                        return Obj("ok", false, "error", "刚刚已保存；请继续游戏，不要重复快速存档。");
                    lastManualSave = DateTime.UtcNow;
                }
                // Same DisplayObject -> GObject -> GList event chain as a native
                // click, including button state changes and game-side checks.
                control.displayObject.BubbleEvent("onClick", new InputEvent());
            }
            else if (kind == "text")
            {
                GTextInput input = control as GTextInput;
                if (input == null || !input.editable || input.displayAsPassword)
                    return Obj("ok", false, "error", "这不是可编辑的普通文本框。");
                string value = (string)action["text"] ?? "";
                if (input.maxLength > 0 && value.Length > input.maxLength)
                    return Obj("ok", false, "error", "文字超过游戏文本框允许的长度。");
                input.text = value;
                input.onChanged.Call();
                if ((bool?)action["submit"] == true) input.onSubmit.Call();
            }
            else if (kind == "scroll")
            {
                GComponent component = control as GComponent;
                if (component == null || component.scrollPane == null)
                    return Obj("ok", false, "error", "这个控件没有滚动区域。");
                float amount = (float?)action["amount"] ?? 0.5f;
                if ((string)action["axis"] == "x") component.scrollPane.SetPercX(component.scrollPane.percX + amount, false);
                else component.scrollPane.SetPercY(component.scrollPane.percY + amount, false);
            }
            else if (kind == "set_value")
            {
                GSlider slider = control as GSlider;
                double value = (double?)action["value"] ?? Double.NaN;
                if (slider == null || Double.IsNaN(value) || value < slider.min || value > slider.max)
                    return Obj("ok", false, "error", "数值不在游戏控件允许的范围内。");
                slider.value = value;
                slider.onChanged.Call();
                slider.onGripTouchEnd.Call();
            }
            else return Obj("ok", false, "error", "Unknown game action.");
            return Obj("ok", true, "message", "已向游戏原有控件提交操作；请观察实际结果。", "observation", Observe());
        }

        private JObject Checkpoint()
        {
            SaveSystem saves = UnityEngine.Object.FindObjectOfType<SaveSystem>();
            GameSceneManager scenes = UnityEngine.Object.FindObjectOfType<GameSceneManager>();
            if (SceneManager.GetActiveScene().name != "MainGame" || (scenes != null && scenes.IsLoading))
                return Obj("ok", true, "saved", false, "message", "当前在菜单或加载中；AI 会话保留，游戏保持打开。");
            BindingFlags fields = BindingFlags.Instance | BindingFlags.NonPublic;
            FieldInfo initialized = typeof(SaveSystem).GetField("isInitialized", fields);
            FieldInfo timeManager = typeof(SaveSystem).GetField("timeManager", fields);
            FieldInfo variables = typeof(SaveSystem).GetField("variableStorage", fields);
            if (saves == null || initialized == null || timeManager == null || variables == null
                || !(bool)initialized.GetValue(saves)
                || (UnityEngine.Object)timeManager.GetValue(saves) == null || (UnityEngine.Object)variables.GetValue(saves) == null)
                return Obj("ok", true, "saved", false, "pending", true, "message", "游戏保存系统正在准备；游戏保持打开。");
            int slotId = saves.GetActiveSlotId();
            SaveSlot slot = saves.GetSlotInfo(slotId);
            string before = slot == null || slot.quickSave == null ? null : slot.quickSave.savedAtUtc;
            saves.RequestQuickSave();
            lastManualSave = DateTime.UtcNow;
            slot = saves.GetSlotInfo(slotId);
            if (slot == null || slot.quickSave == null || slot.quickSave.savedAtUtc == before)
                return Obj("ok", false, "error", "游戏当前不能完成 F5 快速存档；游戏仍保持打开，请在游戏就绪后重试。");
            string file, error;
            RewindSnapshot snapshot = RewindService.ReadSnapshot(slot.quickSave.rewindName, out file, out error);
            if (snapshot == null || RewindGameState.Load(RewindGameState.PathFor(file)) == null)
                return Obj("ok", false, "error", "原生快速存档未能通过读取检查，游戏仍保持打开。");
            MethodInfo settingsMethod = typeof(SaveSystem).GetMethod("GetES3Settings", BindingFlags.Instance | BindingFlags.NonPublic);
            if (settingsMethod == null) return Obj("ok", false, "error", "游戏存档接口与当前适配器不匹配。");
            ES3Settings settings = (ES3Settings)settingsMethod.Invoke(saves, null);
            SaveSlot diskSlot = ES3.Load<SaveSlot>("saveSlot", "SaveData/slot_" + slotId + ".es3", settings);
            if (diskSlot == null || diskSlot.quickSave == null || diskSlot.quickSave.savedAtUtc != slot.quickSave.savedAtUtc)
                return Obj("ok", false, "error", "快速存档的槽位索引未写入，游戏仍保持打开。");
            return Obj("ok", true, "saved", true, "slot", slotId, "saved_at", diskSlot.quickSave.savedAtUtc,
                "message", "已通过原生 F5 流程保存，并读回快照、游戏状态及槽位索引。");
        }

        private void OnDestroy()
        {
            LogLifecycle("OnDestroy", true);
            stopped = true;
            paused = true;
            if (dialogue != null) dialogue.OnLineStarted -= OnLine;
            if (listener != null) listener.Stop();
            try
            {
                if (endpoint != null && File.Exists(endpoint) && (string)JObject.Parse(File.ReadAllText(endpoint))["instance"] == instance)
                    File.Delete(endpoint);
            }
            catch (IOException) { }
        }
    }
}
