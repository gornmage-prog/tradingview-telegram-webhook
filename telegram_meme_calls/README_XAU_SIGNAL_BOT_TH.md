# XAUUSD M5/M15 Signal Bot

บอทตัวนี้เอาไว้ใช้แทน TradingView alerts สำหรับคนใช้แผนฟรี

หลักการ:

- รันบน Render
- ดึงแท่ง `XAU/USD` แบบ `5min` จาก Twelve Data
- รวมแท่ง `15m` และ `1h` เองในเซิร์ฟเวอร์
- คำนวณสัญญาณเน้นๆ สำหรับ `M5` และ `M15`
- ส่งเข้า Telegram ตรงเมื่อแท่งปิด

## ต้องมีอะไรเพิ่ม

ต้องมี `TWELVEDATA_API_KEY` 1 ค่า

## env ที่ใช้

```text
XAU_SIGNAL_BOT_ENABLED=true
TWELVEDATA_API_KEY=your_key_here
XAU_SIGNAL_SYMBOL=XAU/USD
XAU_SIGNAL_OFFSET_SECONDS=20
```

## เช็กสถานะ

เปิด:

```text
https://tradingview-telegram-webhook-voug.onrender.com/healthz
```

ถ้าเปิดใช้งานแล้วจะเห็น `xau_signal_bot.enabled = true`

## จุดเด่นของ logic

- EMA trend alignment
- higher timeframe confirm
- ADX strength filter
- MACD momentum filter
- RSI sweet spot
- breakout close
- candle quality
- session filter เฉพาะช่วงตลาดทองคึก
