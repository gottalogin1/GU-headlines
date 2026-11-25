# Sponsor pictures

Put the picture of the sponsor spot in this folder, then write its file name
in `SPONSOR_IMAGE` in `.env` (for example `SPONSOR_IMAGE=ad.jpg`).

- JPG, PNG, WebP or GIF. A still picture: an animated GIF shows only its first
  picture.
- 600 pixels wide is ideal (the column on the site is 300 pixels wide, and
  phones show pictures at twice the size). A rectangle of 600 x 500 pixels
  (the usual "300 x 250" ad, at twice the size) fits well. The site makes a
  small copy of a bigger picture by itself.
- Git ignores the pictures in this folder, so the automatic update never
  changes or removes them.

The steps, and the other sponsor settings: see the "Sponsor spot" part of
`.env.example` and of `docs/settings.md`.
