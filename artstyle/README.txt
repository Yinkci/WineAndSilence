Put the art style you want for generated scene images in this folder.

- Add PNG, JPG or WebP images with the look you want (your best existing scenes work great).
  StoryCraft sends up to 3 of them with every image request, rotating through all of them.
- Optional text files (one item per line, lines starting with # are ignored):
    style.txt  - describes the look. Replaces StoryCraft's built-in flat-cartoon style.
    avoid.txt  - things never to draw. Replaces the built-in list.
    rules.txt  - extra rules added to every image, for example:
                     Any blood is drawn bright pink, cartoon-style.
    last_scene.txt - extra rules for the last scene of the video only (e.g. a quiet closing image).
  Delete style.txt / avoid.txt to go back to the built-in style.
- Starting a different series? Swap the images and these three files. Other files (like .bak backups) are ignored.
