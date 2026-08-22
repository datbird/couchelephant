# Third-party notices

CouchElephant is otherwise original work. This file records code adapted from
elsewhere, with the licence it came under.

---

## CodeFronts, "Particle Burst" CSS tabs

- **Source:** https://codefronts.com/navigation/css-tabs/particle-burst/
- **Author:** CodeFronts (https://codefronts.com)
- **Licence:** MIT

Used as the tab bar under the header: a sliding underline and an eight-spark
burst. The original vanilla HTML/CSS/JS was adapted into the `.pt-*` rules and
the tab script in `app/templates/base.html`. The underline and the sparks are
themed to CouchElephant's own palette, read from CSS custom properties so they
follow the light and dark themes. The animation technique, per-particle
trajectory through the `--dx` and `--dy` custom properties with the sparks
spawned by script, is unchanged.

These pages are server rendered rather than a single-page app, so the slide is
replayed on load from the tab the reader came from instead of running on click.

### MIT License

```
MIT License

Copyright (c) CodeFronts (https://codefronts.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
