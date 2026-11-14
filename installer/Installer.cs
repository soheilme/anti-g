using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Win32;

static class Installer
{
    const string AppName = "AI Tool";
    const string Version = "1.0.0";
    const string AppExe = "AI-Tool.exe";
    const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\AITool";
    static bool silent = false;

    static string LogPath()
    {
        return Path.Combine(Path.GetTempPath(), "ai-tool-install.log");
    }

    static void Log(string message)
    {
        try
        {
            File.AppendAllText(LogPath(), DateTime.Now.ToString("HH:mm:ss") + "  " + message + Environment.NewLine);
        }
        catch { }
    }

    static void Step(string name, Action action)
    {
        try
        {
            action();
            Log("ok    " + name);
        }
        catch (Exception ex)
        {
            Log("FAIL  " + name + "  -> " + ex.GetType().Name + ": " + ex.Message);
            throw;
        }
    }

    static string TargetDir()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AI Tool");
    }

    static string StartupLnk()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            @"Microsoft\Windows\Start Menu\Programs\Startup\AI Tool Watchdog.lnk");
    }

    static string StartMenuDir()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            @"Microsoft\Windows\Start Menu\Programs\AI Tool");
    }

    static string DesktopLnk()
    {
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "AI Tool.lnk");
    }

    [STAThread]
    static void Main(string[] args)
    {
        bool uninstall = args.Length > 0 && args[0].Equals("/uninstall", StringComparison.OrdinalIgnoreCase);
        silent = Array.Exists(args, delegate(string a) { return a.Equals("/silent", StringComparison.OrdinalIgnoreCase); });
        Log("start  args=" + string.Join(" ", args) + "  silent=" + silent);
        try
        {
            if (uninstall) Uninstall();
            else Install();
            Log("done");
        }
        catch (Exception ex)
        {
            Log("ERROR " + ex.ToString());
            Environment.ExitCode = 1;
            if (!silent)
                MessageBox.Show("خطا: " + ex.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    static void Install()
    {
        string dir = TargetDir();
        string answer = "AI Tool نصب شود؟\n\nمسیر: " + dir + "\n\nتنظیمات فعلی، اگر وجود داشته باشد، دست نمی‌خورد.";
        if (!silent && MessageBox.Show(answer, AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return;

        Log("target=" + dir);
        Step("kill running app", KillApp);
        Step("create folder", delegate { Directory.CreateDirectory(dir); });
        Step("extract app", delegate { Extract("AI-Tool.exe", Path.Combine(dir, AppExe), true); });
        Step("extract ini", delegate { Extract("agtool.ini", Path.Combine(dir, "agtool.ini"), false); });
        Step("extract nodes", delegate { Extract("mlm_found_nodes.json", Path.Combine(dir, "mlm_found_nodes.json"), false); });
        Step("extract readme", delegate { Extract("README.txt", Path.Combine(dir, "README.txt"), true); });

        string exe = Path.Combine(dir, AppExe);
        string icon = exe + ",0";
        string self = Assembly.GetExecutingAssembly().Location;
        string uninstaller = Path.Combine(dir, "uninstall.exe");
        Step("copy uninstaller", delegate { File.Copy(self, uninstaller, true); });

        Step("desktop shortcut", delegate
        {
            MakeShortcut(DesktopLnk(), exe, "", dir, icon, "AI Tool — Google Flow و Antigravity روی VPN");
        });
        Step("start menu folder", delegate { Directory.CreateDirectory(StartMenuDir()); });
        Step("start menu shortcut", delegate
        {
            MakeShortcut(Path.Combine(StartMenuDir(), "AI Tool.lnk"), exe, "", dir, icon,
                "AI Tool — Google Flow و Antigravity روی VPN");
        });
        Step("uninstall shortcut", delegate
        {
            // WScript.Shell cannot save a .lnk whose own file name has Persian letters
            MakeShortcut(Path.Combine(StartMenuDir(), "Uninstall AI Tool.lnk"), uninstaller, "/uninstall", dir, icon,
                "حذف AI Tool");
        });

        Step("registry entry", delegate
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(UninstallKey))
            {
                key.SetValue("DisplayName", AppName);
                key.SetValue("DisplayVersion", Version);
                key.SetValue("Publisher", AppName);
                key.SetValue("DisplayIcon", icon);
                key.SetValue("InstallLocation", dir);
                key.SetValue("UninstallString", "\"" + uninstaller + "\" /uninstall");
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
        });

        Step("start app", delegate { Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = dir }); });

        if (!silent)
            MessageBox.Show("AI Tool نصب شد.\n\n• شورتکات روی دسکتاپ ساخته شد\n• از منوی Start هم اجرا می‌شود\n• برای حذف: تنظیمات ویندوز ← Apps\n\nاول فیلترشکن را روشن کن، بعد در پنجره برنامه پریست فیلترشکن را انتخاب و «اعمال» بزن.",
                AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    static void Uninstall()
    {
        string dir = TargetDir();
        if (!silent && MessageBox.Show("AI Tool حذف شود؟\n\nتنظیمات، تاریخچه و نود ذخیره‌شده هم پاک می‌شوند.",
                AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
            return;

        Log("uninstall target=" + dir);
        KillApp();
        TryDelete(DesktopLnk());
        TryDelete(StartupLnk());
        try { if (File.Exists(uninstallerPath())) TryDelete(uninstallerPath()); } catch { }
        try { if (Directory.Exists(StartMenuDir())) Directory.Delete(StartMenuDir(), true); } catch { }
        try { Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false); } catch { }
        Log("uninstall: shortcuts+registry removed");

        // delete the folder after this process exits
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo("cmd.exe",
                "/c ping -n 3 127.0.0.1 >nul & rd /s /q \"" + dir + "\"");
            psi.CreateNoWindow = true;
            psi.UseShellExecute = false;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(psi);
        }
        catch { }

        if (!silent)
            MessageBox.Show("AI Tool حذف شد.", AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    static string uninstallerPath()
    {
        return Path.Combine(TargetDir(), "uninstall.exe");
    }

    static void KillApp()
    {
        foreach (Process p in Process.GetProcessesByName("AI-Tool"))
        {
            try { p.Kill(); p.WaitForExit(4000); } catch { }
        }
    }

    static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    static void Extract(string resource, string path, bool overwrite)
    {
        if (!overwrite && File.Exists(path)) return;
        using (Stream src = Assembly.GetExecutingAssembly().GetManifestResourceStream(resource))
        {
            if (src == null) throw new Exception("فایل داخلی نصاب پیدا نشد: " + resource);
            using (FileStream dst = File.Create(path)) src.CopyTo(dst);
        }
    }

    static void MakeShortcut(string lnkPath, string target, string arguments, string workingDir,
                             string icon, string description)
    {
        object shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        object shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod,
            null, shell, new object[] { lnkPath });
        Type type = shortcut.GetType();
        type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { target });
        if (!string.IsNullOrEmpty(arguments))
            type.InvokeMember("Arguments", BindingFlags.SetProperty, null, shortcut, new object[] { arguments });
        type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { workingDir });
        if (!string.IsNullOrEmpty(icon))
            type.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { icon });
        if (!string.IsNullOrEmpty(description))
            type.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { description });
        type.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
    }
}
