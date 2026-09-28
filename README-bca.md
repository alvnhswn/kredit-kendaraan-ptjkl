# Aplikasi Digitalisasi Pengajuan Kredit Kendaraan - PT. JKL

Program web untuk mendigitalkan proses pengajuan kredit kendaraan bermotor (Soal 2.a, Tes Coding BCA Finance 2025). Proses manual (fotokopi, scan, print, TTD basah) diganti dengan input online, kontrol kelengkapan dokumen, approval digital, generate kontrak dan PO, TTD digital, verifikasi, dan pencairan dana.

## Teknologi

Python 3, Flask, SQLite (`kredit_v2.db`, dibuat otomatis), Bootstrap 5 (CDN, butuh internet).

## Cara Menjalankan

```
pip install -r requirements.txt
python app.py
```

Buka `http://127.0.0.1:5000`. Database, akun demo, dan data dealer dibuat otomatis.

| Username | Password | Peran |
|---|---|---|
| sales | sales123 | Sales Dealer |
| marketing | marketing123 | Marketing |
| atasan | atasan123 | Atasan Marketing |
| admin | admin123 | Admin Backoffice |

Opsional: `SECRET_KEY` (kunci sesi) dan `FLASK_DEBUG=1` (mode debug) dapat diatur lewat environment variable.

## Alur Proses (sesuai diagram to-be)

1. Sales Dealer membuat pengajuan awal dan upload dokumen (Draft). Marketing mendapat notifikasi.
2. Marketing melengkapi data lalu submit. Submit hanya berhasil jika keempat dokumen (KTP, KK, bukti tanda jadi, form aplikasi) lengkap; jika belum, data tersimpan sebagai Draft.
3. Atasan Marketing approve atau reject (catatan wajib saat reject). Jika ditolak, Marketing merevisi dan submit ulang.
4. Admin Backoffice generate kontrak dan PO; link TTD unik dibuat.
5. Konsumen menandatangani kontrak lewat link (tanpa login). Dealer membuka PO lewat link.
6. Admin Backoffice memverifikasi kontrak ber-TTD, lalu mencairkan dana.

Status: `DRAFT` -> `MENUNGGU_APPROVAL` -> `DISETUJUI` -> `MENUNGGU_TTD` -> `TTD_SELESAI` -> `DOKUMEN_VALID` -> `DANA_CAIR` (cabang `DITOLAK` kembali ke Marketing). Setiap transisi divalidasi di server, sehingga langkah tidak bisa dilompati.

## Fitur

- Hak akses per peran (role-based access control)
- Dashboard ringkasan dan "perlu tindakan Anda", daftar pengajuan dengan pencarian dan filter status
- Form pengajuan: data konsumen, kendaraan, dan pinjaman, dengan validasi (NIK 16 digit, data pasangan wajib jika Kawin, DP < harga) dan angsuran otomatis
- Progress tahapan (stepper), kelengkapan dokumen, dan riwayat status (audit trail) pada detail pengajuan
- Notifikasi dalam aplikasi otomatis saat status berubah
- Kontrak dan PO otomatis dari data (cetak atau simpan PDF lewat browser)
- TTD digital lewat link token

## Database (ternormalisasi)

`users`, `dealer`, `konsumen` (NIK unik), `pengajuan` (FK ke konsumen dan dealer), `dokumen`, `riwayat`, `notifikasi`. Data konsumen dan dealer tidak diulang di setiap pengajuan.

## Keamanan

Password di-hash, seluruh query berparameter (anti SQL injection), proteksi CSRF pada semua form POST, validasi ekstensi file upload (PDF/JPG/PNG) dan batas ukuran 20 MB, akses dokumen wajib login, token TTD acak, serta pengecekan role dan status di setiap aksi.

## Asumsi

- Rumus angsuran: `(harga - DP + asuransi) x (1 + bunga x lama/12) / lama`, bunga flat 10% per tahun (`BUNGA_FLAT` di `app.py`).
- Konsumen dan Dealer tidak memiliki akun; mereka mengakses dokumen lewat link token.

## Batasan

- Kontrak dan PO berupa halaman HTML yang dapat dicetak, bukan PDF server-side.
- Notifikasi hanya di dalam aplikasi (tanpa email/push).
- TTD berupa gambar tanda tangan, bukan tanda tangan elektronik tersertifikasi.
- Login belum dilengkapi pembatasan percobaan (rate limiting); untuk produksi perlu HTTPS dan `SECRET_KEY` sendiri.
