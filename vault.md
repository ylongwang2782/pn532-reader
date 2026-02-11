# Vault APDU Protocol v2

## 指令集

| 功能 | CLA | INS | P1 | P2 | Lc | Data | Le | 说明 |
|------|-----|-----|----|----|----|------|----|------|
| SELECT | 00 | A4 | 04 | 00 | 06 | F0 01 02 03 04 05 | - | 选中 Vault 应用 (AID) |
| WRITE | 00 | D0 | offset_hi | offset_lo | len | bytes | - | 向指定偏移写数据 |
| READ | 00 | B0 | offset_hi | offset_lo | - | - | len | 读取指定偏移数据 |
| GET LENGTH | 80 | CA | 00 | 00 | - | - | - | 返回 2 字节大端有效数据长度 |

## 关键参数

- **AID**: `F0 01 02 03 04 05`
- **缓冲区**: 2048 字节
- **偏移**: P1:P2 组合 16 位大端（0~2047）
- **单次 APDU 数据限制**: ≤32 字节（受 PN7160 I2C@100kHz + FWI=4 制约）
- **分包传输**: 读卡器自动将大数据拆为 32B 分包

## 限制说明

详见 firmware-pro2 仓库 `executables/nfc_test/vault.md`。
