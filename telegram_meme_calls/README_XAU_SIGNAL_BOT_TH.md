# XAUUSD M5/M15 Signal Bot

บอทตัวนี้เอาไว้แทน TradingView alerts สำหรับคนใช้แผนฟรี

หลักการ:
- รันบน Render
- ดึงแท่ง `5m` ของทองจากแหล่งข้อมูลฟรี
- รวมแท่ง `15m` และ `1h` ในเซิร์ฟเวอร์
- คำนวณสัญญาณแบบเน้นคุณภาพสำหรับ `M5` และ `M15`
- ส่งเข้า Telegram อัตโนมัติเมื่อแท่งปิด

## แหล่งข้อมูลที่รองรับ

1. `yahoo`
- ฟรี
- ใช้ `GC=F` เป็น gold futures proxy
- เหมาะสุดถ้าต้องการทางฟรีแบบไม่ต้องสมัคร data plan

2. `stooq`
- ฟรี
- ใช้ได้บางสัญลักษณ์ แต่ `XAU` intraday มักไม่ครบ

3. `twelvedata`
- ใช้คีย์ API
- เหมาะถ้าต้องการ spot data ตรงกว่า แต่แพลนฟรีมักติดสิทธิ์

## env ที่ใช้

แบบฟรีที่แนะนำ:

```text
XAU_SIGNAL_BOT_ENABLED=true
XAU_DATA_PROVIDER=yahoo
YAHOO_GOLD_SYMBOL=GC=F
XAU_SIGNAL_OFFSET_SECONDS=20
XAU_SIGNAL_BACKFILL_HOURS=6
XAU_SIGNAL_MAX_SEND_PER_RUN=6
XAU_SIGNAL_MAX_AGE_M5_MINUTES=20
XAU_SIGNAL_MAX_AGE_M15_MINUTES=45
```

แบบ Stooq:

```text
XAU_SIGNAL_BOT_ENABLED=true
XAU_DATA_PROVIDER=stooq
STOOQ_API_KEY=your_key_here
STOOQ_SYMBOL=xauusd
XAU_SIGNAL_OFFSET_SECONDS=20
```

แบบ Twelve Data:

```text
XAU_SIGNAL_BOT_ENABLED=true
XAU_DATA_PROVIDER=twelvedata
TWELVEDATA_API_KEY=your_key_here
XAU_SIGNAL_SYMBOL=XAU/USD
XAU_SIGNAL_OFFSET_SECONDS=20
```

## เช็กสถานะ

เปิด:

```text
https://tradingview-telegram-webhook-voug.onrender.com/healthz
```

ถ้าบอทเปิดสำเร็จ จะเห็น `xau_signal_bot.enabled = true`

## จุดเด่นของ logic

- EMA trend alignment
- higher timeframe confirm
- ADX strength filter
- MACD momentum filter
- RSI sweet spot
- breakout close
- candle quality
- session filter

หมายเหตุ:
- โหมด `yahoo` ใช้ `GC=F` เป็น proxy ไม่ใช่ spot XAU/USD ตรงจากโบรกเกอร์
- ถ้า feed ของโบรกเกอร์กับ futures ต่างกันเล็กน้อย จุดเข้าอาจไม่เท่ากันเป๊ะ
- เวอร์ชันนี้ดึง `5m`, `15m`, `60m` แยกกันเพื่อให้ `M15` คำนวณได้จริง
- ถ้า Render หลับไปชั่วคราว ระบบจะพยายาม backfill สัญญาณล่าสุดที่ยังไม่ถูกส่งเมื่อมันตื่นกลับมา
- ระบบจะกรองอายุสัญญาณก่อนส่ง เพื่อไม่ให้แจ้งเตือนเก่าเกินไป
- ข้อความจะแสดง `Current Proxy` และ `Signal Age` เพื่อให้เห็นว่าราคาตอนส่งต่างจากราคา entry แค่ไหน
