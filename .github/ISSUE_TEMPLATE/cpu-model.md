---
name: CPU model of a plan
about: What lscpu shows on a server you rent, so the price page can show the hardware behind a plan
labels: hardware
---

Providers rarely say which CPU is behind a plan. If you rent a server from one of the providers on the price page,
this is how to tell everyone else. / اگر از یکی از ارائه‌دهنده‌های صفحه قیمت سرور دارید، این‌طور می‌توانید به بقیه بگویید
پشت آن پلن چه پردازنده‌ای هست.

- Provider and plan / ارائه‌دهنده و پلن:
- Datacenter, if you know it / مرکز داده:
- Date you ran it / تاریخ:
- Output of `lscpu` (at least the "Model name" and "CPU MHz" lines) / خروجی `lscpu`:

```text

```

Nothing in `lscpu` identifies you or your server. If "Model name" is something like `QEMU Virtual CPU`, the host is
hiding the real model: that is worth reporting too.
