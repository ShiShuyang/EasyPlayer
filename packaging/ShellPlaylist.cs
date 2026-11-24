using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using IDataObject = System.Runtime.InteropServices.ComTypes.IDataObject;
using Timer = System.Threading.Timer;

[StructLayout(LayoutKind.Sequential)]
public struct DropPoint
{
    public int X;
    public int Y;
}

[ComVisible(true)]
[Guid("00000122-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IDropTarget
{
    [PreserveSig] int DragEnter(IDataObject data, uint keys, DropPoint point, ref uint effect);
    [PreserveSig] int DragOver(uint keys, DropPoint point, ref uint effect);
    [PreserveSig] int DragLeave();
    [PreserveSig] int Drop(IDataObject data, uint keys, DropPoint point, ref uint effect);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
public sealed class PlaylistDropTarget : IDropTarget
{
    private const uint CopyEffect = 1;
    private readonly Timer idle;

    internal PlaylistDropTarget(Timer idle)
    {
        this.idle = idle;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern uint DragQueryFileW(IntPtr drop, uint index, StringBuilder path, uint capacity);

    [DllImport("ole32.dll")]
    private static extern void ReleaseStgMedium(ref STGMEDIUM medium);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern uint GetTempPathW(uint capacity, StringBuilder path);

    private static string NewManifestPath()
    {
        // Use the same directory spelling as the player, including Windows 8.3 aliases.
        var directory = new StringBuilder(32768);
        uint length = GetTempPathW((uint)directory.Capacity, directory);
        if (length == 0 || length >= directory.Capacity)
            throw new IOException("Cannot resolve the Windows temporary directory.");
        return Path.Combine(directory.ToString(), "EasyPlayerQueue-" + Guid.NewGuid().ToString("N") + ".txt");
    }

    public int DragEnter(IDataObject data, uint keys, DropPoint point, ref uint effect)
    {
        effect &= CopyEffect;
        return 0;
    }

    public int DragOver(uint keys, DropPoint point, ref uint effect)
    {
        effect &= CopyEffect;
        return 0;
    }

    public int DragLeave()
    {
        return 0;
    }

    public int Drop(IDataObject data, uint keys, DropPoint point, ref uint effect)
    {
        string manifest = null;
        try
        {
            FORMATETC format = new FORMATETC {
                cfFormat = 15, // CF_HDROP
                dwAspect = DVASPECT.DVASPECT_CONTENT,
                lindex = -1,
                tymed = TYMED.TYMED_HGLOBAL
            };
            STGMEDIUM medium;
            data.GetData(ref format, out medium);
            try
            {
                uint count = DragQueryFileW(medium.unionmember, uint.MaxValue, null, 0);
                if (count == 0)
                {
                    effect = 0;
                    return 0;
                }

                manifest = NewManifestPath();
                using (var writer = new StreamWriter(new FileStream(manifest, FileMode.CreateNew), new UTF8Encoding(false)))
                {
                    for (uint index = 0; index < count; index++)
                    {
                        uint length = DragQueryFileW(medium.unionmember, index, null, 0);
                        var path = new StringBuilder((int)length + 1);
                        if (DragQueryFileW(medium.unionmember, index, path, (uint)path.Capacity) != 0)
                            writer.Write(path.ToString() + "\n");
                    }
                }
            }
            finally
            {
                ReleaseStgMedium(ref medium);
            }

            var start = new ProcessStartInfo {
                FileName = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "EasyPlayer.exe"),
                Arguments = "--enqueue-from \"" + manifest + "\"",
                UseShellExecute = false
            };
            using (Process.Start(start)) { }
            manifest = null;
            effect = CopyEffect;
            return 0;
        }
        catch (Exception error)
        {
            effect = 0;
            return Marshal.GetHRForException(error);
        }
        finally
        {
            if (manifest != null)
            {
                try { File.Delete(manifest); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            idle.Change(750, Timeout.Infinite);
        }
    }
}

[ComVisible(true)]
[Guid("00000001-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IClassFactory
{
    [PreserveSig] int CreateInstance(IntPtr outer, ref Guid iid, out IntPtr instance);
    [PreserveSig] int LockServer([MarshalAs(UnmanagedType.Bool)] bool locked);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
public sealed class PlaylistClassFactory : IClassFactory
{
    private readonly Timer idle;

    internal PlaylistClassFactory(Timer idle)
    {
        this.idle = idle;
    }

    public int CreateInstance(IntPtr outer, ref Guid iid, out IntPtr instance)
    {
        instance = IntPtr.Zero;
        if (outer != IntPtr.Zero)
            return unchecked((int)0x80040110); // CLASS_E_NOAGGREGATION

        IntPtr target = Marshal.GetIUnknownForObject(new PlaylistDropTarget(idle));
        try { return Marshal.QueryInterface(target, ref iid, out instance); }
        finally { Marshal.Release(target); }
    }

    public int LockServer(bool locked)
    {
        return 0;
    }
}

internal static class ShellPlaylist
{
    [DllImport("ole32.dll")]
    private static extern int CoRegisterClassObject(ref Guid clsid, [MarshalAs(UnmanagedType.IUnknown)] object factory,
        uint context, uint registration, out uint cookie);

    [DllImport("ole32.dll")]
    private static extern int CoRevokeClassObject(uint cookie);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll", ExactSpelling = true)]
    private static extern bool PostThreadMessageW(uint thread, uint message, UIntPtr wParam, IntPtr lParam);

    [STAThread]
    private static int Main(string[] arguments)
    {
        if (arguments.Length != 1 || !string.Equals(arguments[0], "-Embedding", StringComparison.OrdinalIgnoreCase))
            return 0;

        // Initialize the STA before registering the out-of-process shell verb.
        Application.OleRequired();
        uint thread = GetCurrentThreadId();
        const uint QuitMessage = 0x0012; // WM_QUIT
        using (var idle = new Timer(_ => PostThreadMessageW(thread, QuitMessage, UIntPtr.Zero, IntPtr.Zero), null, 60000, Timeout.Infinite))
        {
            var clsid = new Guid("5D96B5DA-F190-4BAC-A8D2-C28237F2489C");
            var factory = new PlaylistClassFactory(idle);
            uint cookie;
            const uint LocalServer = 4;
            const uint SingleUse = 0;
            if (CoRegisterClassObject(ref clsid, factory, LocalServer, SingleUse, out cookie) < 0)
                return 1;

            try
            {
                Application.Run();
            }
            finally
            {
                CoRevokeClassObject(cookie);
                GC.KeepAlive(factory);
            }
        }
        return 0;
    }
}
