using System;
using System.Diagnostics;
using System.Net;
using System.Threading;
using System.Windows.Forms;
using System.IO;
using System.Text.RegularExpressions;
using System.Collections.Generic;

class RouterPanel {
    static Process python;

    static void Main() {
        try {
            string pythonExe = FindPython();
            if (pythonExe == null) {
                Msg("未找到 Python，请安装 Python 3.12+");
                return;
            }

            string routerIp = FindRouter();
            if (routerIp == null) {
                routerIp = Prompt("未发现路由器，请输入路由器 IP", "192.168.2.106");
                if (string.IsNullOrEmpty(routerIp)) return;
            }

            string passwd = Environment.GetEnvironmentVariable("ROUTER_PASSWD");
            if (string.IsNullOrEmpty(passwd)) {
                passwd = Prompt("请输入 SSH 密码", "admin");
                if (string.IsNullOrEmpty(passwd)) return;
            }

            string repoDir = AppDomain.CurrentDomain.BaseDirectory;
            string panelScript = Path.Combine(repoDir, "panel", "router_monitor_ap.py");

            if (!File.Exists(panelScript)) {
                Msg("找不到面板脚本：" + panelScript);
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

            Process.Start("http://127.0.0.1:8787");
            Application.Run();
        } catch (Exception ex) {
            Msg("启动失败：" + ex.Message);
        } finally {
            try { if (python != null) python.Kill(); } catch {}
        }
    }

    static void Msg(string text) {
        MessageBox.Show(text, "路由器面板", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    static string FindPython() {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidates = {
            Path.Combine(local, @"Programs\Python\Python312\python.exe"),
            Path.Combine(local, @"Programs\Python\Python311\python.exe"),
            @"C:\Python312\python.exe",
            @"C:\Python311\python.exe",
            @"C:\Program Files\Python312\python.exe",
            @"C:\Program Files\Python311\python.exe",
        };
        foreach (string exe in candidates) {
            if (File.Exists(exe)) return exe;
        }
        // 最后试 PATH 里的 python
        foreach (string dir in Environment.GetEnvironmentVariable("PATH").Split(';')) {
            try {
                string p = Path.Combine(dir, "python.exe");
                if (File.Exists(p)) return p;
            } catch {}
        }
        return null;
    }

    static string FindRouter() {
        string[] common = { "192.168.31.1", "192.168.2.106", "192.168.2.1", "192.168.1.1", "192.168.0.1" };
        foreach (string ip in common) {
            if (Fingerprint(ip)) return ip;
        }
        string subnet = GetLocalSubnet();
        if (subnet != null) {
            for (int i = 1; i < 255; i++) {
                string ip = subnet + i;
                if (Fingerprint(ip)) return ip;
            }
        }
        return null;
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

        input.Controls.Add(lbl); input.Controls.Add(tb); input.Controls.Add(btn);
        return input.ShowDialog() == DialogResult.OK ? tb.Text : null;
    }
}