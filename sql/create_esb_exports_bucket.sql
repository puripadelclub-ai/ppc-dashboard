-- Bucket privat untuk arsip export mentah ESB (Sales Recapitulation Detail Report).
-- Diisi oleh /api/fetch-esb (service role), dibaca oleh /api/process.
-- Tanpa policy: hanya service_role yang bisa baca/tulis, user dashboard tidak.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'esb-exports', 'esb-exports', false, 20971520,
  array['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']
)
on conflict (id) do nothing;
