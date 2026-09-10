"""Procedural Blender scenes rendered headlessly (no GUI needed).

A scene is a validated :class:`SceneSpec` (meshes, text, lights, camera,
animation, render settings). :func:`render_script` turns it into a
``bpy`` script that reads the scene from a sidecar JSON file (so user
content never touches generated code — no script-injection surface),
and :class:`BlenderRunner` executes it with ``blender --background``.

When no Blender binary is available the runner reports
``available=False`` with install guidance instead of pretending; spec
validation and script generation still work so scenes can be prepared
anywhere and rendered where Blender exists. The generated script is
written against both the Blender 3.x and 4.x ``bpy`` APIs (engine and
socket names are probed, not assumed).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BLENDER_ENV_VAR = "BLENDER_BIN"
DEFAULT_TIMEOUT = 600.0

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MESH_KINDS = frozenset({"cube", "sphere", "cylinder", "cone", "plane",
                         "torus", "monkey"})
_LIGHT_TYPES = frozenset({"sun", "point", "spot", "area"})
_ENGINES = frozenset({"eevee", "cycles", "workbench"})

MAX_OBJECTS = 64
MAX_FRAMES = 600
MAX_RESOLUTION = (7680, 4320)
MIN_RESOLUTION = (16, 16)
MAX_SAMPLES = 4096


class BlenderSpecError(ValueError):
    """The scene description itself is invalid."""


def _check_name(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise BlenderSpecError(
            f"{kind} must match {_NAME_RE.pattern}; got {value!r}")
    return value


def _check_vec(value: Any, kind: str, size: int) -> tuple[float, ...]:
    if (not isinstance(value, (list, tuple)) or len(value) != size
            or not all(isinstance(item, (int, float)) for item in value)):
        raise BlenderSpecError(
            f"{kind} must be a {size}-tuple of numbers; got {value!r}")
    return tuple(float(item) for item in value)


def _check_color(value: Any, kind: str) -> tuple[float, float, float]:
    rgb = _check_vec(value, kind, 3)
    if any(channel < 0.0 or channel > 1.0 for channel in rgb):
        raise BlenderSpecError(f"{kind} channels must be 0..1; got {value!r}")
    return (rgb[0], rgb[1], rgb[2])


@dataclass(frozen=True)
class MaterialSpec:
    """Principled-BSDF material (base color + PBR scalars + emission)."""

    name: str
    color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    metallic: float = 0.0
    roughness: float = 0.5
    emission: tuple[float, float, float] = (0.0, 0.0, 0.0)
    emission_strength: float = 0.0

    def validated(self) -> MaterialSpec:
        _check_name(self.name, "material name")
        _check_color(self.color, "material color")
        _check_color(self.emission, "material emission")
        for label, value in (("metallic", self.metallic),
                             ("roughness", self.roughness)):
            if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                raise BlenderSpecError(
                    f"material {label} must be 0..1; got {value!r}")
        if (not isinstance(self.emission_strength, (int, float))
                or not 0.0 <= self.emission_strength <= 100.0):
            raise BlenderSpecError(
                "emission_strength must be 0..100; "
                f"got {self.emission_strength!r}")
        return self


@dataclass(frozen=True)
class MeshSpec:
    kind: str
    name: str
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    material: MaterialSpec | None = None
    spin: tuple[float, float, float] | None = None
    drift: tuple[float, float, float] | None = None

    def validated(self) -> MeshSpec:
        if self.kind not in _MESH_KINDS:
            raise BlenderSpecError(
                f"mesh kind must be one of {sorted(_MESH_KINDS)}; "
                f"got {self.kind!r}")
        _check_name(self.name, "object name")
        _check_vec(self.location, "location", 3)
        _check_vec(self.rotation, "rotation", 3)
        scale = _check_vec(self.scale, "scale", 3)
        if any(axis <= 0.0 for axis in scale):
            raise BlenderSpecError(f"scale axes must be > 0; got {scale!r}")
        if self.material is not None:
            self.material.validated()
        if self.spin is not None:
            _check_vec(self.spin, "spin", 3)
        if self.drift is not None:
            _check_vec(self.drift, "drift", 3)
        return self


@dataclass(frozen=True)
class TextSpec:
    name: str
    body: str
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    size: float = 1.0
    material: MaterialSpec | None = None

    def validated(self) -> TextSpec:
        _check_name(self.name, "object name")
        if not isinstance(self.body, str) or not self.body.strip():
            raise BlenderSpecError("text body must be a non-empty string")
        if len(self.body) > 500:
            raise BlenderSpecError("text body is limited to 500 characters")
        _check_vec(self.location, "location", 3)
        _check_vec(self.rotation, "rotation", 3)
        if (not isinstance(self.size, (int, float))
                or not 0.01 <= self.size <= 100.0):
            raise BlenderSpecError(
                f"text size must be 0.01..100; got {self.size!r}")
        if self.material is not None:
            self.material.validated()
        return self


@dataclass(frozen=True)
class LightSpec:
    name: str
    light_type: str = "sun"
    location: tuple[float, float, float] = (0.0, 0.0, 5.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    energy: float = 5.0
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def validated(self) -> LightSpec:
        _check_name(self.name, "object name")
        if self.light_type not in _LIGHT_TYPES:
            raise BlenderSpecError(
                f"light type must be one of {sorted(_LIGHT_TYPES)}; "
                f"got {self.light_type!r}")
        _check_vec(self.location, "location", 3)
        _check_vec(self.rotation, "rotation", 3)
        if (not isinstance(self.energy, (int, float))
                or not 0.0 <= self.energy <= 100000.0):
            raise BlenderSpecError(
                f"light energy must be 0..100000; got {self.energy!r}")
        _check_color(self.color, "light color")
        return self


@dataclass(frozen=True)
class CameraSpec:
    name: str = "Camera"
    location: tuple[float, float, float] = (7.0, -7.0, 5.0)
    rotation: tuple[float, float, float] = (1.1, 0.0, 0.7853982)
    lens: float = 50.0
    track: str = ""

    def validated(self, object_names: set[str]) -> CameraSpec:
        _check_name(self.name, "camera name")
        _check_vec(self.location, "location", 3)
        _check_vec(self.rotation, "rotation", 3)
        if not isinstance(self.lens, (int, float)) or not 1.0 <= self.lens <= 500.0:
            raise BlenderSpecError(
                f"camera lens must be 1..500mm; got {self.lens!r}")
        if self.track and self.track not in object_names:
            raise BlenderSpecError(
                f"camera tracks unknown object {self.track!r}")
        return self


@dataclass(frozen=True)
class SceneSpec:
    """A fully validated, render-ready procedural scene."""

    name: str
    meshes: tuple[MeshSpec, ...] = ()
    texts: tuple[TextSpec, ...] = ()
    lights: tuple[LightSpec, ...] = ()
    camera: CameraSpec = field(default_factory=CameraSpec)
    resolution: tuple[int, int] = (1280, 720)
    frame_start: int = 1
    frame_end: int = 1
    engine: str = "eevee"
    samples: int = 64
    background: tuple[float, float, float] = (0.05, 0.05, 0.08)
    output: str = "render"

    @property
    def animated(self) -> bool:
        return self.frame_end > self.frame_start

    def validated(self) -> SceneSpec:
        _check_name(self.name, "scene name")
        total = len(self.meshes) + len(self.texts) + len(self.lights)
        if total == 0:
            raise BlenderSpecError("scene must contain at least one object")
        if total > MAX_OBJECTS:
            raise BlenderSpecError(
                f"scene is limited to {MAX_OBJECTS} objects; got {total}")
        names = [obj.name for obj in
                 (*self.meshes, *self.texts, *self.lights)]
        if len(set(names)) != len(names):
            raise BlenderSpecError(f"object names must be unique: {names!r}")
        for mesh in self.meshes:
            mesh.validated()
        for text in self.texts:
            text.validated()
        for light in self.lights:
            light.validated()
        self.camera.validated(set(names) | {self.camera.name})
        width, height = self.resolution
        if (not isinstance(width, int) or not isinstance(height, int)
                or not MIN_RESOLUTION[0] <= width <= MAX_RESOLUTION[0]
                or not MIN_RESOLUTION[1] <= height <= MAX_RESOLUTION[1]):
            raise BlenderSpecError(
                f"resolution must be within {MIN_RESOLUTION}.."
                f"{MAX_RESOLUTION}; got {self.resolution!r}")
        if (not isinstance(self.frame_start, int)
                or not isinstance(self.frame_end, int)
                or self.frame_start < 0
                or self.frame_end < self.frame_start
                or self.frame_end - self.frame_start + 1 > MAX_FRAMES):
            raise BlenderSpecError(
                f"frame range must span 1..{MAX_FRAMES} frames; got "
                f"{self.frame_start}..{self.frame_end}")
        if self.engine not in _ENGINES:
            raise BlenderSpecError(
                f"engine must be one of {sorted(_ENGINES)}; "
                f"got {self.engine!r}")
        if (not isinstance(self.samples, int)
                or not 1 <= self.samples <= MAX_SAMPLES):
            raise BlenderSpecError(
                f"samples must be 1..{MAX_SAMPLES}; got {self.samples!r}")
        _check_color(self.background, "background")
        _check_name(self.output, "output basename")
        return self

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (also the sidecar format Blender reads)."""
        def material_dict(material: MaterialSpec | None) -> dict[str, Any] | None:
            if material is None:
                return None
            return {"name": material.name, "color": list(material.color),
                    "metallic": material.metallic,
                    "roughness": material.roughness,
                    "emission": list(material.emission),
                    "emission_strength": material.emission_strength}

        return {
            "name": self.name,
            "meshes": [{
                "kind": mesh.kind, "name": mesh.name,
                "location": list(mesh.location),
                "rotation": list(mesh.rotation),
                "scale": list(mesh.scale),
                "material": material_dict(mesh.material),
                "spin": list(mesh.spin) if mesh.spin else None,
                "drift": list(mesh.drift) if mesh.drift else None,
            } for mesh in self.meshes],
            "texts": [{
                "name": text.name, "body": text.body,
                "location": list(text.location),
                "rotation": list(text.rotation), "size": text.size,
                "material": material_dict(text.material),
            } for text in self.texts],
            "lights": [{
                "name": light.name, "type": light.light_type,
                "location": list(light.location),
                "rotation": list(light.rotation),
                "energy": light.energy, "color": list(light.color),
            } for light in self.lights],
            "camera": {
                "name": self.camera.name,
                "location": list(self.camera.location),
                "rotation": list(self.camera.rotation),
                "lens": self.camera.lens, "track": self.camera.track},
            "resolution": list(self.resolution),
            "frame_start": self.frame_start, "frame_end": self.frame_end,
            "engine": self.engine, "samples": self.samples,
            "background": list(self.background), "output": self.output,
        }


def _material_from(data: Any) -> MaterialSpec | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise BlenderSpecError(f"material must be an object; got {data!r}")
    try:
        return MaterialSpec(
            name=data.get("name", "Material"),
            color=tuple(data.get("color", (0.8, 0.8, 0.8))),
            metallic=data.get("metallic", 0.0),
            roughness=data.get("roughness", 0.5),
            emission=tuple(data.get("emission", (0.0, 0.0, 0.0))),
            emission_strength=data.get("emission_strength", 0.0),
        ).validated()
    except (AttributeError, TypeError) as exc:
        raise BlenderSpecError(f"invalid material: {exc}") from exc


def build_scene(data: dict[str, Any]) -> SceneSpec:
    """Parse and validate a scene from a JSON-shaped dict."""
    if not isinstance(data, dict):
        raise BlenderSpecError("scene must be a JSON object")

    def objects(key: str, factory: Any) -> tuple:
        items = data.get(key, ())
        if not isinstance(items, (list, tuple)):
            raise BlenderSpecError(f"scene.{key} must be a list")
        return tuple(factory(item) for item in items)

    def mesh_from(item: Any) -> MeshSpec:
        if not isinstance(item, dict):
            raise BlenderSpecError(f"mesh must be an object; got {item!r}")
        spin = item.get("spin")
        drift = item.get("drift")
        return MeshSpec(
            kind=item.get("kind", "cube"), name=item.get("name", "Object"),
            location=tuple(item.get("location", (0.0, 0.0, 0.0))),
            rotation=tuple(item.get("rotation", (0.0, 0.0, 0.0))),
            scale=tuple(item.get("scale", (1.0, 1.0, 1.0))),
            material=_material_from(item.get("material")),
            spin=tuple(spin) if spin else None,
            drift=tuple(drift) if drift else None).validated()

    def text_from(item: Any) -> TextSpec:
        if not isinstance(item, dict):
            raise BlenderSpecError(f"text must be an object; got {item!r}")
        return TextSpec(
            name=item.get("name", "Text"),
            body=item.get("body", ""),
            location=tuple(item.get("location", (0.0, 0.0, 0.0))),
            rotation=tuple(item.get("rotation", (0.0, 0.0, 0.0))),
            size=item.get("size", 1.0),
            material=_material_from(item.get("material"))).validated()

    def light_from(item: Any) -> LightSpec:
        if not isinstance(item, dict):
            raise BlenderSpecError(f"light must be an object; got {item!r}")
        return LightSpec(
            name=item.get("name", "Light"),
            light_type=item.get("type", "sun"),
            location=tuple(item.get("location", (0.0, 0.0, 5.0))),
            rotation=tuple(item.get("rotation", (0.0, 0.0, 0.0))),
            energy=item.get("energy", 5.0),
            color=tuple(item.get("color", (1.0, 1.0, 1.0)))).validated()

    camera_data = data.get("camera", {})
    if not isinstance(camera_data, dict):
        raise BlenderSpecError("scene.camera must be an object")
    camera = CameraSpec(
        name=camera_data.get("name", "Camera"),
        location=tuple(camera_data.get("location", (7.0, -7.0, 5.0))),
        rotation=tuple(camera_data.get("rotation", (1.1, 0.0, 0.7853982))),
        lens=camera_data.get("lens", 50.0),
        track=camera_data.get("track", ""))
    resolution = data.get("resolution", (1280, 720))
    background = data.get("background", (0.05, 0.05, 0.08))
    try:
        return SceneSpec(
            name=data.get("name", "scene"),
            meshes=objects("meshes", mesh_from),
            texts=objects("texts", text_from),
            lights=objects("lights", light_from),
            camera=camera,
            resolution=(int(resolution[0]), int(resolution[1])),
            frame_start=int(data.get("frame_start", 1)),
            frame_end=int(data.get("frame_end", 1)),
            engine=data.get("engine", "eevee"),
            samples=int(data.get("samples", 64)),
            background=tuple(background),
            output=data.get("output", "render"),
        ).validated()
    except (KeyError, TypeError, IndexError) as exc:
        raise BlenderSpecError(f"invalid scene: {exc}") from exc


def example_scene() -> dict[str, Any]:
    """A small turntable scene usable as a starting point / smoke test."""
    return {
        "name": "forge_example",
        "meshes": [
            {"kind": "cube", "name": "Hero",
             "location": [0.0, 0.0, 1.0],
             "material": {"name": "HeroMat", "color": [0.1, 0.4, 0.9],
                         "metallic": 0.6, "roughness": 0.3},
             "spin": [0.0, 0.0, 6.28318]},
            {"kind": "plane", "name": "Floor",
             "scale": [10.0, 10.0, 1.0],
             "material": {"name": "FloorMat", "color": [0.15, 0.15, 0.18],
                         "roughness": 0.9}},
        ],
        "texts": [{"name": "Title", "body": "FORGE",
                   "location": [-2.5, 0.0, 3.0], "size": 1.2,
                   "material": {"name": "TitleMat",
                               "color": [1.0, 1.0, 1.0],
                               "emission": [1.0, 0.6, 0.1],
                               "emission_strength": 2.0}}],
        "lights": [{"name": "Sun", "type": "sun", "energy": 5.0}],
        "camera": {"track": "Hero", "lens": 50.0},
        "resolution": [1280, 720],
        "frame_start": 1, "frame_end": 1,
        "engine": "eevee", "samples": 64,
        "background": [0.05, 0.05, 0.08],
        "output": "forge_example",
    }


# The bpy program is static: every scene-specific value arrives through the
# sidecar JSON file, so scene content can never inject code. It probes engine
# and socket names so one script serves Blender 3.x and 4.x.
RENDER_SCRIPT = '''"""Forge-generated bpy scene builder (reads SPEC_JSON, writes render)."""
import json
import sys

import bpy


def _spec_path():
    args = sys.argv
    marker = args.index("--")
    return args[marker + 1], args[marker + 2]


def _link_material(obj, spec):
    mat = bpy.data.materials.new(name=spec["name"])
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    principled = nodes.get("Principled BSDF")
    if principled is not None:
        color = spec["color"] + [1.0]
        principled.inputs["Base Color"].default_value = color
        principled.inputs["Metallic"].default_value = spec["metallic"]
        principled.inputs["Roughness"].default_value = spec["roughness"]

        def _set(names, value):
            for name in names:
                socket = principled.inputs.get(name)
                if socket is not None:
                    socket.default_value = value
                    return

        _set(("Emission Color", "Emission"),
             spec["emission"] + [1.0])
        _set(("Emission Strength",),
             spec["emission_strength"])
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat


def _linearize(obj):
    if obj.animation_data is None or obj.animation_data.action is None:
        return
    for curve in obj.animation_data.action.fcurves:
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


_MESH_OPS = {
    "cube": bpy.ops.mesh.primitive_cube_add,
    "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
    "cylinder": bpy.ops.mesh.primitive_cylinder_add,
    "cone": bpy.ops.mesh.primitive_cone_add,
    "plane": bpy.ops.mesh.primitive_plane_add,
    "torus": bpy.ops.mesh.primitive_torus_add,
    "monkey": bpy.ops.mesh.primitive_monkey_add,
}


def _add_mesh(item):
    _MESH_OPS[item["kind"]](location=item["location"],
                            rotation=item["rotation"],
                            scale=item["scale"])
    obj = bpy.context.active_object
    obj.name = item["name"]
    return obj


def _add_text(item):
    bpy.ops.object.text_add(location=item["location"],
                            rotation=item["rotation"])
    obj = bpy.context.active_object
    obj.name = item["name"]
    obj.data.body = item["body"]
    obj.data.size = item["size"]
    return obj


def _add_light(item):
    kind = {"sun": "SUN", "point": "POINT", "spot": "SPOT",
            "area": "AREA"}[item["type"]]
    bpy.ops.object.light_add(type=kind, location=item["location"],
                             rotation=item["rotation"])
    obj = bpy.context.active_object
    obj.name = item["name"]
    obj.data.energy = item["energy"]
    obj.data.color = item["color"]
    return obj


def main():
    spec_file, out_base = _spec_path()
    with open(spec_file, "r", encoding="utf-8") as handle:
        spec = json.load(handle)

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    scene = bpy.context.scene
    scene.render.resolution_x = spec["resolution"][0]
    scene.render.resolution_y = spec["resolution"][1]
    scene.render.resolution_percentage = 100
    scene.frame_start = spec["frame_start"]
    scene.frame_end = spec["frame_end"]
    if spec["engine"] == "cycles":
        scene.render.engine = "CYCLES"
        scene.cycles.samples = spec["samples"]
        scene.cycles.seed = 0
        scene.cycles.use_denoising = True
    elif spec["engine"] == "workbench":
        scene.render.engine = "BLENDER_WORKBENCH"
    else:
        for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                scene.render.engine = candidate
                break
            except TypeError:
                continue
        scene.eevee.taa_render_samples = spec["samples"]

    world = scene.world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (
            spec["background"] + [1.0])

    built = {}
    for item in spec["meshes"]:
        obj = _add_mesh(item)
        if item["material"]:
            _link_material(obj, item["material"])
        if spec["frame_end"] > spec["frame_start"]:
            if item["spin"]:
                obj.keyframe_insert(data_path="rotation_euler",
                                    frame=spec["frame_start"])
                obj.rotation_euler = [
                    a + b for a, b in zip(obj.rotation_euler,
                                          item["spin"])]
                obj.keyframe_insert(data_path="rotation_euler",
                                    frame=spec["frame_end"])
            if item["drift"]:
                obj.keyframe_insert(data_path="location",
                                    frame=spec["frame_start"])
                obj.location = [a + b for a, b in zip(obj.location,
                                                      item["drift"])]
                obj.keyframe_insert(data_path="location",
                                    frame=spec["frame_end"])
            _linearize(obj)
        built[item["name"]] = obj
    for item in spec["texts"]:
        obj = _add_text(item)
        if item["material"]:
            _link_material(obj, item["material"])
        built[item["name"]] = obj
    for item in spec["lights"]:
        built[item["name"]] = _add_light(item)

    camera_spec = spec["camera"]
    bpy.ops.object.camera_add(location=camera_spec["location"],
                              rotation=camera_spec["rotation"])
    camera = bpy.context.active_object
    camera.name = camera_spec["name"]
    camera.data.lens = camera_spec["lens"]
    scene.camera = camera
    if camera_spec["track"] and camera_spec["track"] in built:
        constraint = camera.constraints.new(type="TRACK_TO")
        constraint.target = built[camera_spec["track"]]
        constraint.track_axis = "TRACK_NEGATIVE_Z"
        constraint.up_axis = "UP_Y"

    scene.render.filepath = out_base
    scene.render.image_settings.file_format = "PNG"
    if spec["frame_end"] > spec["frame_start"]:
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        bpy.ops.render.render(animation=True)
    else:
        bpy.ops.render.render(write_still=True)
    print(f"FORGE_RENDER_DONE {out_base}")


main()
'''


def render_script(spec: SceneSpec) -> str:
    """Return the static bpy program for a validated scene."""
    spec.validated()
    return RENDER_SCRIPT


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one headless render (honest about availability)."""

    available: bool
    success: bool
    spec_name: str
    blender: str = ""
    blender_version: str = ""
    output_files: tuple[str, ...] = ()
    elapsed_s: float = 0.0
    stdout_tail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available, "success": self.success,
            "spec_name": self.spec_name, "blender": self.blender,
            "blender_version": self.blender_version,
            "output_files": list(self.output_files),
            "elapsed_s": round(self.elapsed_s, 3),
            "stdout_tail": self.stdout_tail, "error": self.error,
        }


def find_blender(explicit: str | None = None) -> str | None:
    """Locate a Blender binary: explicit path, $BLENDER_BIN, or PATH."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_bin = os.environ.get(BLENDER_ENV_VAR, "").strip()
    if env_bin:
        candidates.append(env_bin)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    which = shutil.which("blender")
    return which


def blender_version(blender: str, *, timeout: float = 30.0) -> str:
    """Return the `Blender X.Y.Z` banner line (raises on failure)."""
    proc = subprocess.run(
        [blender, "--version"], capture_output=True, text=True,
        timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"blender --version failed: {(proc.stderr or '').strip()[:300]}")
    first = (proc.stdout or "").splitlines()
    return first[0].strip() if first else "Blender (unknown version)"


class BlenderRunner:
    """Execute validated scenes through headless Blender."""

    def __init__(self, blender: str | None = None) -> None:
        self.blender = find_blender(blender)

    @property
    def available(self) -> bool:
        return self.blender is not None

    def check(self) -> dict[str, Any]:
        """Availability report with version or install guidance."""
        if self.blender is None:
            return {
                "available": False,
                "blender": "",
                "hint": ("No Blender binary found. Install Blender 3.x/4.x "
                         "(https://www.blender.org/download/) and ensure "
                         "`blender` is on PATH, or set $BLENDER_BIN."),
            }
        try:
            version = blender_version(self.blender)
        except Exception as exc:
            return {"available": False, "blender": self.blender,
                    "hint": f"Blender binary failed to run: {exc}"}
        return {"available": True, "blender": self.blender,
                "version": version}

    def render(self, spec: SceneSpec, out_dir: str | Path, *,
               timeout: float = DEFAULT_TIMEOUT) -> RenderResult:
        """Render one scene; validates first, even when Blender is missing."""
        started = time.monotonic()
        spec.validated()
        if timeout <= 0:
            raise BlenderSpecError("timeout must be positive")
        root = Path(out_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        work = root / ".forge-blender"
        work.mkdir(parents=True, exist_ok=True)
        sidecar = work / f"{spec.name}.json"
        sidecar.write_text(json.dumps(spec.to_dict(), indent=2),
                           encoding="utf-8")
        program = work / f"{spec.name}.py"
        program.write_text(RENDER_SCRIPT, encoding="utf-8")
        out_base = str(root / spec.output)
        if self.blender is None:
            return RenderResult(
                available=False, success=False, spec_name=spec.name,
                elapsed_s=time.monotonic() - started,
                error=("Blender is not installed; scene validated and "
                       f"script written to {program}. Install Blender "
                       "3.x/4.x and re-run to render."))
        try:
            proc = subprocess.run(
                [self.blender, "--background", "--factory-startup",
                 "--python", str(program), "--", str(sidecar), out_base],
                capture_output=True, text=True, timeout=timeout,
                check=False)
        except subprocess.TimeoutExpired:
            return RenderResult(
                available=True, success=False, spec_name=spec.name,
                blender=self.blender,
                elapsed_s=time.monotonic() - started,
                error=f"Blender render exceeded {timeout:.0f}s and was killed.")
        outputs = sorted(str(path) for path in root.glob(f"{spec.output}*")
                         if path.is_file()
                         and not str(path).startswith(str(work)))
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
        if proc.returncode != 0:
            return RenderResult(
                available=True, success=False, spec_name=spec.name,
                blender=self.blender, elapsed_s=time.monotonic() - started,
                stdout_tail=tail,
                error=(f"Blender exited with code {proc.returncode}; "
                       f"see stdout_tail."))
        if not outputs:
            return RenderResult(
                available=True, success=False, spec_name=spec.name,
                blender=self.blender, elapsed_s=time.monotonic() - started,
                stdout_tail=tail,
                error=("Blender exited 0 but produced no output files; "
                       "see stdout_tail."))
        try:
            version = blender_version(self.blender)
        except Exception:
            version = ""
        return RenderResult(
            available=True, success=True, spec_name=spec.name,
            blender=self.blender, blender_version=version,
            output_files=tuple(outputs),
            elapsed_s=time.monotonic() - started, stdout_tail=tail[-1000:])
