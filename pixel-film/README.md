# Pixel film: The Day the Desk Got a Mail Slot

A 21-second pixel-art film made entirely in code. One Python script draws every frame on a
320x180 canvas, upscales it to 1080p and synthesises the music and sound effects. There are no
image or audio assets. The story comes from the publication
[Claudius's mailbox](https://publications.barcik.training/claudius-mailbox).

It was written by an AI coding agent (Claude) in conversation with Robert, and is here as an
example of what "ask an agent for a film" looks like when the result has to be reproducible.

## Files

- `make_film.py`: the whole film (drawing, animation, sound, encoding)
- `claudius-mail-slot.mp4`, `claudius-mail-slot.gif`: the rendered film
- `poster.png`, `claudius-sheet.png`: poster frame and character sheet
- `index.html`: a small page that presents the film; open it in a browser

## Render it yourself

Needs Python with Pillow and numpy, and `ffmpeg` on the path.

```
python make_film.py film <frames_dir> <out_dir>
python make_film.py times
```

The first command writes the frames, the sound, the mp4, the gif, the poster and the sheet.
The second prints the beat timeline. `check` and `seq` produce contact sheets of single frames
for reviewing motion; see the docstring at the top of the script.
