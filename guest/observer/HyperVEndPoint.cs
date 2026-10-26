using System.Net;
using System.Net.Sockets;

namespace Ludius.Observer;

// SOCKADDR_HV: family, reserved, VM id, service id (36 bytes).
sealed class HyperVEndPoint(Guid vmId, Guid serviceId) : EndPoint
{
    public const AddressFamily Family = (AddressFamily)34; // AF_HYPERV
    public const ProtocolType RawProtocol = (ProtocolType)1; // HV_PROTOCOL_RAW

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
