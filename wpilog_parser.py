"""
WPILog (.wpilog) binary file parser.
Format spec: https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/doc/datalog.adoc
"""
import struct


class WPILogParser:
    """Parses WPILib DataLog (.wpilog) binary files."""

    def parse(self, filepath: str) -> tuple:
        """
        Parse a wpilog file.

        Returns:
            (records, extra_header)
            records: dict of {field_name: [(timestamp_sec, value), ...]}
            extra_header: str metadata from file header (e.g. "AdvantageKit")
        """
        with open(filepath, 'rb') as f:
            data = f.read()

        if len(data) < 12 or data[:6] != b'WPILOG':
            raise ValueError(f"Not a valid WPILog file: {filepath}")

        extra_len = struct.unpack_from('<I', data, 8)[0]
        extra_header = data[12:12 + extra_len].decode('utf-8', errors='replace').strip('\x00')
        offset = 12 + extra_len

        entries = {}   # entry_id (int) -> {name: str, type: str}
        records = {}   # field_name (str) -> [(ts_sec, value)]

        while offset < len(data) - 2:
            if offset >= len(data):
                break

            h = data[offset]
            offset += 1

            id_size = (h & 0x3) + 1
            payload_size_size = ((h >> 2) & 0x3) + 1
            timestamp_size = ((h >> 4) & 0x7) + 1

            needed = id_size + payload_size_size + timestamp_size
            if offset + needed > len(data):
                break

            entry_id = self._read_varint(data, offset, id_size)
            offset += id_size
            payload_len = self._read_varint(data, offset, payload_size_size)
            offset += payload_size_size
            timestamp = self._read_varint(data, offset, timestamp_size)
            offset += timestamp_size

            if offset + payload_len > len(data):
                break

            payload = data[offset:offset + payload_len]
            offset += payload_len

            if entry_id == 0:
                self._handle_control(payload, entries)
            elif entry_id in entries:
                entry = entries[entry_id]
                val = self._decode_value(payload, entry['type'])
                if val is not None:
                    name = entry['name']
                    if name not in records:
                        records[name] = []
                    records[name].append((timestamp / 1_000_000.0, val))

        return records, extra_header

    def _handle_control(self, payload: bytes, entries: dict):
        """Process a control record (entry ID = 0)."""
        if not payload:
            return
        ctrl_type = payload[0]
        if ctrl_type == 0 and len(payload) >= 13:  # Start record
            try:
                p = 1
                new_id = struct.unpack_from('<I', payload, p)[0]
                p += 4
                name_len = struct.unpack_from('<I', payload, p)[0]
                p += 4
                if p + name_len > len(payload):
                    return
                name = payload[p:p + name_len].decode('utf-8', errors='replace')
                p += name_len
                if p + 4 > len(payload):
                    return
                type_len = struct.unpack_from('<I', payload, p)[0]
                p += 4
                if p + type_len > len(payload):
                    return
                dtype = payload[p:p + type_len].decode('utf-8', errors='replace')
                entries[new_id] = {'name': name, 'type': dtype}
            except (struct.error, UnicodeDecodeError):
                pass

    @staticmethod
    def _read_varint(data: bytes, offset: int, num_bytes: int) -> int:
        val = 0
        for i in range(num_bytes):
            if offset + i < len(data):
                val |= data[offset + i] << (8 * i)
        return val

    @staticmethod
    def _decode_value(payload: bytes, dtype: str):
        """Decode a payload bytes into a Python value based on dtype string."""
        if not payload:
            return None
        try:
            if dtype == 'double':
                return struct.unpack_from('<d', payload)[0] if len(payload) >= 8 else None
            elif dtype == 'float':
                return struct.unpack_from('<f', payload)[0] if len(payload) >= 4 else None
            elif dtype == 'int64':
                return struct.unpack_from('<q', payload)[0] if len(payload) >= 8 else None
            elif dtype == 'boolean':
                return bool(payload[0])
            elif dtype == 'string':
                return payload.decode('utf-8', errors='replace')
            elif dtype == 'string[]':
                strings = []
                p = 0
                while p + 4 <= len(payload):
                    slen = struct.unpack_from('<I', payload, p)[0]
                    p += 4
                    if p + slen > len(payload):
                        break
                    strings.append(payload[p:p + slen].decode('utf-8', errors='replace'))
                    p += slen
                return strings
            elif dtype == 'double[]':
                count = len(payload) // 8
                return list(struct.unpack_from(f'<{count}d', payload[:count * 8]))
            elif dtype == 'float[]':
                count = len(payload) // 4
                return list(struct.unpack_from(f'<{count}f', payload[:count * 4]))
            elif dtype == 'int64[]':
                count = len(payload) // 8
                return list(struct.unpack_from(f'<{count}q', payload[:count * 8]))
            elif dtype == 'boolean[]':
                return [bool(b) for b in payload]
        except Exception:
            pass
        return None
