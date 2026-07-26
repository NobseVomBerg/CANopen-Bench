# Third-party notices

CANopen Bench itself is MIT-licensed (see `LICENSE`). It bundles and
depends on the components below, which keep their own licenses. This
file is the attribution required by those licenses; it travels with the
wheel, the sdist and the Docker image.

## Bundled in this repository

Shipped as source inside the package (`canopen_bench/static/vendor/`),
so these notices apply to every copy of CANopen Bench:

| Component | License | Copyright |
|---|---|---|
| [Preact](https://preactjs.com) | MIT | © 2015-present Jason Miller |
| [htm](https://github.com/developit/htm) | Apache-2.0 | © 2018 Jason Miller |

Both are combined into the single bundle
`canopen_bench/static/vendor/preact-htm.module.js`, which carries the
same notices in its file header. The bundle is unmodified upstream
output — CANopen Bench adds no code to it.

### MIT (Preact)

> Permission is hereby granted, free of charge, to any person obtaining a
> copy of this software and associated documentation files (the
> "Software"), to deal in the Software without restriction, including
> without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to
> permit persons to whom the Software is furnished to do so, subject to
> the following conditions:
>
> The above copyright notice and this permission notice shall be included
> in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
> OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
> MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
> IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
> CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
> TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
> SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

### Apache-2.0 (htm)

Licensed under the Apache License, Version 2.0. You may obtain a copy of
the License at <http://www.apache.org/licenses/LICENSE-2.0>. Unless
required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
OR CONDITIONS OF ANY KIND, either express or implied. See the License for
the specific language governing permissions and limitations under it.

## Runtime dependencies

Installed by pip, not vendored — but a Docker image built from this
repository contains them, and distributing that image distributes them:

| Package | License |
|---|---|
| [python-can](https://github.com/hardbyte/python-can) | **LGPL-3.0-only** |
| [canopen](https://github.com/christiansandberg/canopen) | MIT |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT |

**python-can is LGPL-3.0.** CANopen Bench imports it as an unmodified
library and neither copies nor modifies its source, so the LGPL's
obligations attach to python-can alone, not to CANopen Bench. If you
redistribute a Docker image or any other bundle built from this
repository, you are conveying python-can and must pass on its license
text and let recipients replace it with a modified version — with a
pip-installed package that is already the case, since it can simply be
reinstalled.

## Plugin packages

Plugins are separate distributions with their own licensing; installing
one does not change the license of this package. The public
[`bench-cpcusb`](plugins/bench-cpcusb/)
is MIT. Check each plugin's own `LICENSE` before redistributing an
environment that contains it — a copyleft or proprietary plugin puts its
own obligations on that environment.

## Trademarks

CANopen® and CiA® are registered trademarks of
[CAN in Automation e.V.](https://www.can-cia.org/) CANopen Bench is an
independent project and is neither affiliated with, endorsed by, nor
certified by CAN in Automation. References to CiA specifications
(CiA 301, CiA 305, CiA 402, …) describe which published behaviour this
tool implements; they are not a claim of conformance, and this tool is
not a CiA conformance test tool.

IXXAT® is a trademark of HMS Networks. PCAN® is a trademark of
PEAK-System Technik GmbH. CPC-USB is a product of EMS Dr. Thomas Wünsche.
All other product names are the property of their respective owners and
are used here solely to state which hardware this software interoperates
with. Their use implies no affiliation with or endorsement by those
companies.
