using System;
using System.Diagnostics;
using System.Net;
using System.Threading;
using System.Windows.Forms;
using System.IO;
using System.Text.RegularExpressions;

class RouterPanel {
    static Process python;
    static string foundIp = null;
    internal static int scanDone = 0;

    static void Main() {
        try {
            string pythonExe = FindPython();
            if (pythonExe == null) {
                Msg("未找到 Python，请安装 Python 3.12+");
                return;
            }

            string passwd = Environment.GetEnvironmentVariable("ROUTER_PASSWD");
            if (string.IsNullOrEmpty(passwd)) {
                passwd = Prompt("请输入路由器 SSH 密码", "admin");
                if (string.IsNullOrEmpty(passwd)) return;
            }

            ScanForm scanForm = new ScanForm();
            scanForm.Show();
            Thread scanThread = new Thread(FindRouter);
            scanThread.Start();
            Application.Run(scanForm);
            scanThread.Join(5000);

            string routerIp = foundIp;
            if (routerIp == null) {
                routerIp = Prompt("未发现路由器，请输入路由器 IP", "192.168.2.106");
                if (string.IsNullOrEmpty(routerIp)) return;
            }

            string repoDir = AppDomain.CurrentDomain.BaseDirectory;
            string panelScript = Path.Combine(repoDir, "panel", "router_monitor_ap.py");

            if (!File.Exists(panelScript)) {
                Msg("找不到面板脚本：" + panelScript + "\n请将 exe 放在仓库根目录运行");
                return;
            }

            python = new Process();
            python.StartInfo.FileName = pythonExe;
            python.StartInfo.Arguments = "\"" + panelScript + "\" --host " + routerIp + " --passwd " + passwd;
            python.StartInfo.WindowStyle = ProcessWindowStyle.Hidden;
            python.StartInfo.CreateNoWindow = true;
            python.Start();

            for (int i = 0; i < 30; i++) {
                try {
                    var req = WebRequest.Create("http://127.0.0.1:8787/api");
                    req.GetResponse().Close();
                    break;
                } catch { Thread.Sleep(1000); }
            }

            // 用 Edge app 模式打开独立窗口，等待关闭后清理
            var edge = Process.Start(@"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                "--app=http://127.0.0.1:8787");
            if (edge != null) edge.WaitForExit();
        } catch (Exception ex) {
            Msg("启动失败：" + ex.Message);
        } finally {
            try { if (python != null && !python.HasExited) python.Kill(); } catch {}
        }
    }

    static void Msg(string text) {
        MessageBox.Show(text, "路由器面板", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    static string FindPython() {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] paths = {
            Path.Combine(local, @"Programs\Python\Python312\python.exe"),
            Path.Combine(local, @"Programs\Python\Python311\python.exe"),
            @"C:\Python312\python.exe", @"C:\Python311\python.exe",
            @"C:\Program Files\Python312\python.exe",
            @"C:\Program Files\Python311\python.exe",
        };
        foreach (string p in paths) {
            if (File.Exists(p)) return p;
        }
        foreach (string dir in Environment.GetEnvironmentVariable("PATH").Split(';')) {
            try {
                string p = Path.Combine(dir.Trim(), "python.exe");
                if (File.Exists(p)) return p;
            } catch {}
        }
        return null;
    }

    static void FindRouter() {
        // 先试常见 IP（并行）
        string[] common = { "192.168.31.1", "192.168.2.1", "192.168.1.1",
                            "192.168.0.1", "192.168.2.106", "192.168.2.100" };
        int done = 0;
        foreach (string ip in common) {
            ThreadPool.QueueUserWorkItem(_ => {
                if (Fingerprint(ip) && Interlocked.Exchange(ref scanDone, 1) == 0)
                    foundIp = ip;
                Interlocked.Increment(ref done);
            });
        }
        while (done < common.Length && scanDone == 0) Thread.Sleep(100);

        if (scanDone == 1) return;

        // 扫本地子网（并行）
        string subnet = GetLocalSubnet();
        if (subnet == null) return;
        for (int i = 1; i < 255; i++) {
            string ip = subnet + i;
            ThreadPool.QueueUserWorkItem(_ => {
                if (Fingerprint(ip) && Interlocked.Exchange(ref scanDone, 1) == 0)
                    foundIp = ip;
            });
        }
        // 等最多 15 秒
        for (int w = 0; w < 150 && scanDone == 0; w++) Thread.Sleep(100);
    }

    static bool Fingerprint(string ip) {
        try {
            var req = (HttpWebRequest)WebRequest.Create("http://" + ip + "/cgi-bin/luci/web/home");
            req.Timeout = 1500;
            using (var resp = req.GetResponse())
            using (var sr = new StreamReader(resp.GetResponseStream())) {
                string html = sr.ReadToEnd();
                return html.Contains("XiaoQiang") || html.Contains("miwifi") ||
                       Regex.IsMatch(html, "hardware\\s*=\\s*'RN");
            }
        } catch { return false; }
    }

    static string GetLocalSubnet() {
        try {
            using (var s = new System.Net.Sockets.UdpClient()) {
                s.Connect("223.5.5.5", 80);
                var ep = (System.Net.IPEndPoint)s.Client.LocalEndPoint;
                string ip = ep.Address.ToString();
                return ip.Substring(0, ip.LastIndexOf('.') + 1);
            }
        } catch { return null; }
    }

    static string Prompt(string text, string def) {
        var input = new Form();
        input.Width = 400; input.Height = 140;
        input.FormBorderStyle = FormBorderStyle.FixedDialog;
        input.StartPosition = FormStartPosition.CenterScreen;
        input.Text = "路由器面板";
        input.MaximizeBox = false; input.MinimizeBox = false;
        var lbl = new Label(); lbl.Text = text; lbl.Left = 12; lbl.Top = 12; lbl.Width = 360;
        var tb = new TextBox(); tb.Left = 12; tb.Top = 36; tb.Width = 360; tb.Text = def;
        var btn = new Button(); btn.Text = "确定"; btn.Left = 160; btn.Top = 68; btn.Width = 80;
        btn.DialogResult = DialogResult.OK;
        input.AcceptButton = btn;
        input.Controls.Add(lbl); input.Controls.Add(tb); input.Controls.Add(btn);
        return input.ShowDialog() == DialogResult.OK ? tb.Text : null;
    }
}

class ScanForm : Form {
    Label status;
    System.Windows.Forms.Timer timer;
    public ScanForm() {
        this.Width = 360; this.Height = 100;
        this.FormBorderStyle = FormBorderStyle.FixedDialog;
        this.StartPosition = FormStartPosition.CenterScreen;
        this.Text = "路由器面板";
        this.ControlBox = false;
        status = new Label();
        status.Text = "正在扫描路由器...";
        status.TextAlign = System.Drawing.ContentAlignment.MiddleCenter;
        status.Dock = System.Windows.Forms.DockStyle.Fill;
        status.Font = new System.Drawing.Font("Microsoft YaHei", 11);
        this.Controls.Add(status);
        timer = new System.Windows.Forms.Timer();
        timer.Interval = 200;
        timer.Tick += (s, e) => {
            if (RouterPanel.scanDone == 1) {
                timer.Stop();
                this.Close();
            }
        };
        timer.Start();
    }
    protected override void OnFormClosing(FormClosingEventArgs e) {
        if (RouterPanel.scanDone == 0) e.Cancel = true;
        base.OnFormClosing(e);
    }
}