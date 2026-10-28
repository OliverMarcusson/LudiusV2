using System.Net;
using System.Net.Sockets;

namespace Ludius.Observer;

// SOCKADDR_VM for the virtio-win viosock Winsock provider (Proxmox guests): family, reserved, port, cid, zero[4].
// The provider registers its own address family; 40 (AF_VSOCK) unless `netsh winsock show catalog` says otherwise
// (--vsock-family).
sealed class VsockEndPoint(AddressFamily family, uint cid, uint port) : EndPoint
{
    public const uint AnyCid = 0xFFFFFFFF; // VMADDR_CID_ANY

    public override AddressFamily AddressFamily => family;

    public override SocketAddress Serialize()
    {
        var a = new SocketAddress(family, 16);
        BitConverter.TryWriteBytes(a.Buffer.Span[4..8], port);
        BitConverter.TryWriteBytes(a.Buffer.Span[8..12], cid);
        return a;
    }

    public override EndPoint Create(SocketAddress a) =>
        new VsockEndPoint(family, BitConverter.ToUInt32(a.Buffer.Span[8..12]), BitConverter.ToUInt32(a.Buffer.Span[4..8]));

    public override string ToString() => $"vsock:{cid}/{port}";
}
