using System.Net.Sockets;
using System.Runtime.InteropServices;

namespace Ludius.Observer;

sealed partial class VsockListener : IDisposable
{
    const uint AnyCid = 0xFFFFFFFF;
    static readonly nint Invalid = -1;
    readonly nint s;

    public VsockListener(int family, uint port)
    {
        var wsa = new byte[512];
        if (WSAStartup(0x0202, wsa) != 0) throw new SocketException(WSAGetLastError());
        s = WSASocketW(family, 1 , 0, 0, 0, 0);
        if (s == Invalid) throw new SocketException(WSAGetLastError());
        var a = new byte[16];
        BitConverter.TryWriteBytes(a.AsSpan(0, 2), (ushort)family);
        BitConverter.TryWriteBytes(a.AsSpan(4, 4), port);
        BitConverter.TryWriteBytes(a.AsSpan(8, 4), AnyCid);
        if (bind(s, a, a.Length) != 0 || listen(s, 1) != 0)
        {
            var e = WSAGetLastError();
            closesocket(s);
            throw new SocketException(e);
        }
    }

    public Stream Accept()
    {
        var c = accept(s, 0, 0);
        if (c == Invalid) throw new SocketException(WSAGetLastError());
        return new VsockStream(c);
    }

    public void Dispose() => closesocket(s);

    sealed class VsockStream(nint c) : Stream
    {
        int closed;

        public override unsafe int Read(byte[] buffer, int offset, int count)
        {
            if (count == 0) return 0;
            fixed (byte* p = &buffer[offset])
            {
                var n = recv(c, p, count, 0);
                if (n < 0) throw new IOException("vsock recv", new SocketException(WSAGetLastError()));
                return n;
            }
        }

        public override unsafe void Write(byte[] buffer, int offset, int count)
        {
            while (count > 0)
            {
                fixed (byte* p = &buffer[offset])
                {
                    var n = send(c, p, count, 0);
                    if (n < 0) throw new IOException("vsock send", new SocketException(WSAGetLastError()));
                    offset += n;
                    count -= n;
                }
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (Interlocked.Exchange(ref closed, 1) == 0) closesocket(c);
            base.Dispose(disposing);
        }

        public override void Flush() { }
        public override bool CanRead => true;
        public override bool CanWrite => true;
        public override bool CanSeek => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
    }

    [LibraryImport("ws2_32.dll")] private static partial int WSAStartup(ushort version, byte[] data);
    [LibraryImport("ws2_32.dll")] private static partial int WSAGetLastError();
    [LibraryImport("ws2_32.dll")] private static partial nint WSASocketW(int af, int type, int protocol, nint info, uint group, uint flags);
    [LibraryImport("ws2_32.dll")] private static partial int bind(nint s, byte[] name, int len);
    [LibraryImport("ws2_32.dll")] private static partial int listen(nint s, int backlog);
    [LibraryImport("ws2_32.dll")] private static partial nint accept(nint s, nint addr, nint len);
    [LibraryImport("ws2_32.dll")] private static unsafe partial int recv(nint s, byte* buf, int len, int flags);
    [LibraryImport("ws2_32.dll")] private static unsafe partial int send(nint s, byte* buf, int len, int flags);
    [LibraryImport("ws2_32.dll")] private static partial int closesocket(nint s);
}
