using System.Net;
using System.Net.Sockets;

namespace Ludius.Observer;

sealed class HyperVEndPoint(Guid vmId, Guid serviceId) : EndPoint
{
    public const AddressFamily Family = (AddressFamily)34;
    public const ProtocolType RawProtocol = (ProtocolType)1;

    public Guid VmId { get; } = vmId;
    public Guid ServiceId { get; } = serviceId;
    public override AddressFamily AddressFamily => Family;

    public override SocketAddress Serialize()
    {
        var a = new SocketAddress(Family, 36);
        VmId.TryWriteBytes(a.Buffer.Span[4..20]);
        ServiceId.TryWriteBytes(a.Buffer.Span[20..36]);
        return a;
    }

    public override EndPoint Create(SocketAddress a) =>
        new HyperVEndPoint(new Guid(a.Buffer.Span[4..20]), new Guid(a.Buffer.Span[20..36]));

    public override string ToString() => $"hv:{VmId}/{ServiceId}";
}
