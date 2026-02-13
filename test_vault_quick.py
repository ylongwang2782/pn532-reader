#!/usr/bin/env python3
"""Quick Vault APDU v2 test for PRO1 (PN532) and PRO2 (PN7160)."""
import sys
import time
sys.path.insert(0, '.')
from pn532 import PN532

def extract_data(result):
    """Extract raw bytes from read_vault_tag result dict."""
    if result is None:
        return None
    if isinstance(result, dict):
        hex_str = result.get('data_hex', '')
        if hex_str:
            return bytes.fromhex(hex_str.replace(' ', ''))
        return b''
    return result

def test_vault():
    pn = PN532()
    pn._ensure_open()
    pn._wakeup()
    pn.sam_configuration()
    
    print("=" * 60)
    print("Vault APDU v2 Quick Test")
    print("=" * 60)
    
    # Test 1: Basic write + read at offset 0
    print("\n[Test 1] Write 'Hello Vault!' at offset 0")
    test_data = b"Hello Vault!"
    result = pn.write_vault_tag(0, test_data)
    if result is None:
        print("  ❌ WRITE FAILED")
        return False
    print(f"  Write OK, {result['bytes_written']} bytes written")
    
    time.sleep(0.3)
    
    raw = extract_data(pn.read_vault_tag(0, len(test_data)))
    if raw is None:
        print("  ❌ READ FAILED")
        return False
    if raw == test_data:
        print(f"  ✅ Read back matches: {raw}")
    else:
        print(f"  ❌ MISMATCH: expected {test_data.hex()}, got {raw.hex()}")
        return False
    
    # Test 2: 16-bit offset write + read at offset 300
    print("\n[Test 2] Write 'OFFSET_TEST' at offset 300")
    test_data2 = b"OFFSET_TEST"
    result2 = pn.write_vault_tag(300, test_data2)
    if result2 is None:
        print("  ❌ WRITE FAILED")
        return False
    print(f"  Write OK, {result2['bytes_written']} bytes written")
    
    time.sleep(0.3)
    
    raw2 = extract_data(pn.read_vault_tag(300, len(test_data2)))
    if raw2 is None:
        print("  ❌ READ FAILED")
        return False
    if raw2 == test_data2:
        print(f"  ✅ Read back matches: {raw2}")
    else:
        print(f"  ❌ MISMATCH: expected {test_data2.hex()}, got {raw2.hex()}")
        return False
    
    # Test 3: GET DATA LENGTH (should be 311 = 300 + 11)
    print("\n[Test 3] GET DATA LENGTH")
    length = pn.get_vault_length()
    if length is None:
        print("  ❌ GET LENGTH FAILED")
        return False
    expected_len = 300 + len(test_data2)  # 311
    if length == expected_len:
        print(f"  ✅ Length = {length} (expected {expected_len})")
    else:
        print(f"  ⚠️  Length = {length} (expected {expected_len})")
    
    # Test 4: Larger write (128 bytes) at offset 0
    print("\n[Test 4] Write 128 bytes at offset 0")
    test_data3 = bytes(range(128))
    result3 = pn.write_vault_tag(0, test_data3)
    if result3 is None:
        print("  ❌ WRITE FAILED")
        return False
    print(f"  Write OK, {result3['bytes_written']} bytes written in {result3.get('chunks', '?')} chunks")
    
    time.sleep(0.3)
    
    raw3 = extract_data(pn.read_vault_tag(0, 128))
    if raw3 is None:
        print("  ❌ READ FAILED")
        return False
    if raw3 == test_data3:
        print(f"  ✅ Read back matches (128 bytes)")
    else:
        for i in range(min(len(raw3), len(test_data3))):
            if raw3[i] != test_data3[i]:
                print(f"  ❌ MISMATCH at byte {i}: expected 0x{test_data3[i]:02X}, got 0x{raw3[i]:02X}")
                break
        return False
    
    # Test 5: Verify offset 300 data is still intact
    print("\n[Test 5] Verify offset 300 data integrity")
    raw4 = extract_data(pn.read_vault_tag(300, len(test_data2)))
    if raw4 is None:
        print("  ❌ READ FAILED")
        return False
    if raw4 == test_data2:
        print(f"  ✅ Data at offset 300 still intact: {raw4}")
    else:
        print(f"  ❌ MISMATCH: expected {test_data2.hex()}, got {raw4.hex()}")
        return False
    
    # Test 6: GET DATA LENGTH after all writes
    print("\n[Test 6] Final GET DATA LENGTH")
    final_len = pn.get_vault_length()
    if final_len is None:
        print("  ❌ GET LENGTH FAILED")
        return False
    print(f"  ✅ Final length = {final_len}")
    
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)
    return True

if __name__ == "__main__":
    success = test_vault()
    sys.exit(0 if success else 1)
