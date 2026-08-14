$ErrorActionPreference = 'Stop'

if (-not ('TarkovCis.VpnGateNativeCatalogReader' -as [type])) {
    $source = @'
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace TarkovCis
{
    public sealed class VpnGateNativeRow
    {
        public string CountryShort { get; set; }
        public string CountryLong { get; set; }
        public string Fqdn { get; set; }
        public string IP { get; set; }
        public string SslPorts { get; set; }
        public ulong PingToJapan { get; set; }
        public ulong SpeedToJapan { get; set; }
        public ulong Score { get; set; }
        public ulong NumSessions { get; set; }
    }

    public sealed class VpnGateNativeCatalog
    {
        public string Path { get; set; }
        public DateTime TimestampUtc { get; set; }
        public double AgeHours { get; set; }
        public bool SignaturePresent { get; set; }
        public bool Compressed { get; set; }
        public int RowCount { get; set; }
        public List<VpnGateNativeRow> Rows { get; set; }
    }

    internal sealed class PackElement
    {
        public uint Type;
        public object[] Values;
    }

    internal sealed class PackReader
    {
        private readonly byte[] bytes;
        private readonly int maximumElements;
        private readonly int maximumValues;
        private readonly int maximumValueBytes;
        private int position;

        public PackReader(byte[] bytes, int maximumElements, int maximumValues, int maximumValueBytes)
        {
            this.bytes = bytes;
            this.maximumElements = maximumElements;
            this.maximumValues = maximumValues;
            this.maximumValueBytes = maximumValueBytes;
        }

        private void Require(int count, string context)
        {
            if (count < 0 || position < 0 || position > bytes.Length || count > bytes.Length - position)
                throw new InvalidDataException("SoftEther PACK ended while reading " + context + ".");
        }

        private uint ReadUInt32(string context)
        {
            Require(4, context);
            uint value = ((uint)bytes[position] << 24) |
                         ((uint)bytes[position + 1] << 16) |
                         ((uint)bytes[position + 2] << 8) |
                         bytes[position + 3];
            position += 4;
            return value;
        }

        private ulong ReadUInt64(string context)
        {
            Require(8, context);
            ulong value = 0;
            for (int i = 0; i < 8; i++) value = (value << 8) | bytes[position + i];
            position += 8;
            return value;
        }

        private byte[] ReadBytes(int count, string context)
        {
            Require(count, context);
            byte[] value = new byte[count];
            if (count > 0) Buffer.BlockCopy(bytes, position, value, 0, count);
            position += count;
            return value;
        }

        private string ReadElementName()
        {
            uint encodedLength = ReadUInt32("element name length");
            if (encodedLength < 2 || encodedLength > 64)
                throw new InvalidDataException("SoftEther PACK element name length is invalid.");
            byte[] raw = ReadBytes((int)encodedLength - 1, "element name");
            return Encoding.ASCII.GetString(raw);
        }

        private string ReadString(string context, bool trimTerminator)
        {
            uint length = ReadUInt32(context + " length");
            if (length > maximumValueBytes)
                throw new InvalidDataException("SoftEther PACK " + context + " is too large.");
            string value = Encoding.UTF8.GetString(ReadBytes((int)length, context));
            return trimTerminator ? value.TrimEnd('\0') : value;
        }

        public Dictionary<string, PackElement> Read()
        {
            uint elementCount = ReadUInt32("element count");
            if (elementCount < 1 || elementCount > maximumElements)
                throw new InvalidDataException("SoftEther PACK element count is invalid.");

            Dictionary<string, PackElement> result = new Dictionary<string, PackElement>(StringComparer.Ordinal);
            for (uint elementIndex = 0; elementIndex < elementCount; elementIndex++)
            {
                string name = ReadElementName();
                if (String.IsNullOrEmpty(name) || result.ContainsKey(name))
                    throw new InvalidDataException("SoftEther PACK contains an invalid or duplicate element name.");
                uint type = ReadUInt32(name + " type");
                uint valueCount = ReadUInt32(name + " value count");
                if (type > 4) throw new InvalidDataException("SoftEther PACK element has an unsupported type.");
                if (valueCount < 1 || valueCount > maximumValues)
                    throw new InvalidDataException("SoftEther PACK element has an invalid value count.");

                object[] values = new object[valueCount];
                for (uint valueIndex = 0; valueIndex < valueCount; valueIndex++)
                {
                    switch (type)
                    {
                        case 0:
                            values[valueIndex] = ReadUInt32(name);
                            break;
                        case 1:
                            uint dataLength = ReadUInt32(name + " data length");
                            if (dataLength > maximumValueBytes)
                                throw new InvalidDataException("SoftEther PACK data element is too large.");
                            values[valueIndex] = ReadBytes((int)dataLength, name);
                            break;
                        case 2:
                            values[valueIndex] = ReadString(name, false);
                            break;
                        case 3:
                            values[valueIndex] = ReadString(name, true);
                            break;
                        case 4:
                            values[valueIndex] = ReadUInt64(name);
                            break;
                    }
                }
                result.Add(name, new PackElement { Type = type, Values = values });
            }
            return result;
        }
    }

    public static class VpnGateNativeCatalogReader
    {
        private static byte[] ReadFileShared(string path)
        {
            Exception lastError = null;
            for (int attempt = 0; attempt < 3; attempt++)
            {
                try
                {
                    using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    {
                        if (stream.Length <= 0 || stream.Length > 16 * 1024 * 1024)
                            throw new InvalidDataException("VPN Gate native catalog size is invalid.");
                        byte[] bytes = new byte[(int)stream.Length];
                        int read = 0;
                        while (read < bytes.Length)
                        {
                            int count = stream.Read(bytes, read, bytes.Length - read);
                            if (count <= 0) throw new EndOfStreamException("VPN Gate native catalog ended unexpectedly.");
                            read += count;
                        }
                        return bytes;
                    }
                }
                catch (Exception ex)
                {
                    lastError = ex;
                    if (attempt < 2) System.Threading.Thread.Sleep(150);
                }
            }
            throw lastError;
        }

        private static uint ReadUInt32(byte[] bytes, int offset)
        {
            if (offset < 0 || bytes.Length - offset < 4) throw new InvalidDataException("UInt32 is outside the payload.");
            return ((uint)bytes[offset] << 24) |
                   ((uint)bytes[offset + 1] << 16) |
                   ((uint)bytes[offset + 2] << 8) |
                   bytes[offset + 3];
        }

        private static byte[] Slice(byte[] bytes, int offset, int count)
        {
            if (offset < 0 || count < 0 || offset > bytes.Length || count > bytes.Length - offset)
                throw new InvalidDataException("VPN Gate payload slice is invalid.");
            byte[] result = new byte[count];
            if (count > 0) Buffer.BlockCopy(bytes, offset, result, 0, count);
            return result;
        }

        private static object Value(Dictionary<string, PackElement> pack, string name, int index)
        {
            PackElement element;
            if (!pack.TryGetValue(name, out element) || index < 0 || index >= element.Values.Length) return null;
            return element.Values[index];
        }

        private static string StringValue(Dictionary<string, PackElement> pack, string name, int index)
        {
            object value = Value(pack, name, index);
            return value == null ? String.Empty : Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        private static ulong UInt64Value(Dictionary<string, PackElement> pack, string name, int index)
        {
            object value = Value(pack, name, index);
            return value == null ? 0UL : Convert.ToUInt64(value, CultureInfo.InvariantCulture);
        }

        private static DateTime ReadTimestamp(byte[] fileBytes, string path)
        {
            string header = Encoding.ASCII.GetString(fileBytes, 0, Math.Min(240, fileBytes.Length));
            Match match = Regex.Match(header, @"(?m)^(?<Timestamp>\d{8}_\d{6}\.\d{3})\r?$");
            DateTime timestamp;
            if (match.Success && DateTime.TryParseExact(match.Groups["Timestamp"].Value, "yyyyMMdd_HHmmss.fff",
                CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out timestamp))
                return timestamp;
            return File.GetLastWriteTimeUtc(path);
        }

        private static byte[] ExpandPayload(byte[] payload)
        {
            if (payload.Length < 11) throw new InvalidDataException("Compressed VPN Gate payload is too short.");
            uint expectedLength = ReadUInt32(payload, 0);
            if (expectedLength < 1 || expectedLength > 32 * 1024 * 1024)
                throw new InvalidDataException("Compressed VPN Gate payload length is invalid.");

            int deflateOffset = 6;
            int deflateLength = payload.Length - deflateOffset - 4;
            if (deflateLength < 1) throw new InvalidDataException("Compressed VPN Gate zlib stream is invalid.");
            using (MemoryStream input = new MemoryStream(payload, deflateOffset, deflateLength, false))
            using (DeflateStream deflate = new DeflateStream(input, CompressionMode.Decompress))
            using (MemoryStream output = new MemoryStream())
            {
                deflate.CopyTo(output);
                byte[] expanded = output.ToArray();
                if (expanded.Length != expectedLength)
                    throw new InvalidDataException("Compressed VPN Gate payload length does not match its envelope.");
                return expanded;
            }
        }

        public static byte[] TransformRc4(byte[] data, byte[] key)
        {
            if (data == null) throw new ArgumentNullException("data");
            if (key == null || key.Length < 1) throw new ArgumentException("RC4 key is empty.", "key");
            byte[] state = new byte[256];
            for (int i = 0; i < state.Length; i++) state[i] = (byte)i;
            int j = 0;
            for (int i = 0; i < state.Length; i++)
            {
                j = (j + state[i] + key[i % key.Length]) & 255;
                byte swap = state[i]; state[i] = state[j]; state[j] = swap;
            }
            byte[] result = new byte[data.Length];
            int x = 0; j = 0;
            for (int position = 0; position < data.Length; position++)
            {
                x = (x + 1) & 255;
                j = (j + state[x]) & 255;
                byte swap = state[x]; state[x] = state[j]; state[j] = swap;
                result[position] = (byte)(data[position] ^ state[(state[x] + state[j]) & 255]);
            }
            return result;
        }

        public static VpnGateNativeCatalog Read(string path, int maxAgeHours)
        {
            if (String.IsNullOrEmpty(path) || !File.Exists(path))
                throw new FileNotFoundException("VPN Gate native catalog was not found.", path);
            byte[] fileBytes = ReadFileShared(path);
            if (fileBytes.Length <= 0x104) throw new InvalidDataException("VPN Gate native catalog is too short.");
            string marker = Encoding.ASCII.GetString(fileBytes, 0, Math.Min(32, fileBytes.Length));
            if (!marker.StartsWith("[VPNGate Data File]", StringComparison.Ordinal))
                throw new InvalidDataException("VPN Gate native catalog header is invalid.");

            DateTime timestampUtc = ReadTimestamp(fileBytes, path).ToUniversalTime();
            TimeSpan age = DateTime.UtcNow - timestampUtc;
            if (age.TotalMinutes < -60) throw new InvalidDataException("VPN Gate native catalog timestamp is unexpectedly in the future.");
            if (maxAgeHours > 0 && age.TotalHours > maxAgeHours)
                throw new InvalidDataException(String.Format(CultureInfo.InvariantCulture,
                    "VPN Gate native catalog is stale ({0:N1} hours old).", age.TotalHours));

            byte[] seed = Slice(fileBytes, 0xF0, 20);
            byte[] key;
            using (SHA1 sha1 = SHA1.Create()) key = sha1.ComputeHash(seed);
            byte[] encrypted = Slice(fileBytes, 0x104, fileBytes.Length - 0x104);
            byte[] plain = TransformRc4(encrypted, key);
            Dictionary<string, PackElement> outer = new PackReader(plain, 64, 16, 16 * 1024 * 1024).Read();

            byte[] payload = Value(outer, "data", 0) as byte[];
            byte[] signature = Value(outer, "sign", 0) as byte[];
            ulong declaredSize = UInt64Value(outer, "data_size", 0);
            uint compressed = Convert.ToUInt32(Value(outer, "compressed", 0), CultureInfo.InvariantCulture);
            if (payload == null || payload.Length < 4) throw new InvalidDataException("VPN Gate native catalog contains no server payload.");
            if (signature == null || signature.Length != 128) throw new InvalidDataException("VPN Gate native catalog signature marker is invalid.");
            if (declaredSize != 0 && declaredSize != (ulong)payload.Length)
                throw new InvalidDataException("VPN Gate native catalog payload size does not match its envelope.");
            if (compressed != 0) payload = ExpandPayload(payload);

            Dictionary<string, PackElement> inner = new PackReader(payload, 128, 4096, 4 * 1024 * 1024).Read();
            string[] required = { "CountryShort", "IP", "SslPorts" };
            foreach (string name in required)
                if (!inner.ContainsKey(name)) throw new InvalidDataException("VPN Gate native catalog is missing " + name + ".");

            int rowCount = inner["CountryShort"].Values.Length;
            List<VpnGateNativeRow> rows = new List<VpnGateNativeRow>(rowCount);
            for (int index = 0; index < rowCount; index++)
            {
                rows.Add(new VpnGateNativeRow
                {
                    CountryShort = StringValue(inner, "CountryShort", index),
                    CountryLong = StringValue(inner, "CountryFull", index),
                    Fqdn = StringValue(inner, "Fqdn", index),
                    IP = StringValue(inner, "IP", index),
                    SslPorts = StringValue(inner, "SslPorts", index),
                    PingToJapan = UInt64Value(inner, "PingToJapan", index),
                    SpeedToJapan = UInt64Value(inner, "SpeedToJapan", index),
                    Score = UInt64Value(inner, "Score", index),
                    NumSessions = UInt64Value(inner, "NumClients", index)
                });
            }

            return new VpnGateNativeCatalog
            {
                Path = Path.GetFullPath(path),
                TimestampUtc = timestampUtc,
                AgeHours = Math.Max(0, Math.Round(age.TotalHours, 2)),
                SignaturePresent = true,
                Compressed = compressed != 0,
                RowCount = rowCount,
                Rows = rows
            };
        }
    }
}
'@
    Add-Type -TypeDefinition $source -Language CSharp
}

function Invoke-VgRc4 {
    param([Parameter(Mandatory = $true)][byte[]]$Data, [Parameter(Mandatory = $true)][byte[]]$Key)
    return ,([TarkovCis.VpnGateNativeCatalogReader]::TransformRc4($Data, $Key))
}

function Read-VpnGateNativeCatalog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$MaxAgeHours = 24
    )
    [TarkovCis.VpnGateNativeCatalogReader]::Read($Path, $MaxAgeHours)
}
