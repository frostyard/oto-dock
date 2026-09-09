/**
 * Camera-pose math for the 3D map (components/agents-map/camera.ts): the
 * viewScale clamp, the whole-map pose, and the stage-exit flight.
 *
 * Live-hit 2026-09-09 (operator): zooming out of a department landed the
 * camera, then visibly zoomed out AGAIN. The exit flight kept its own copy of
 * the whole-map distance without the viewScale factor the paged pose applies
 * (1.38 on a 1920×1080 window), so the flight landed short and the paged
 * machinery lerped the camera out afterwards. One pose function now serves
 * both, and the flight's end IS that pose.
 */
import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  EXIT_ARC_LIFT,
  EXIT_ARC_MIN_RADIUS,
  MAP_DIST_MAX,
  MAP_DIST_MIN,
  MAP_ELEVATION,
  VIEW_SCALE_MAX,
  mapDistanceFor,
  mapPoseFor,
  stageExitPath,
  viewScaleFor,
} from '@/components/agents-map/camera'
import type { MapCluster } from '@/components/agents-map/layout'

const cluster = (outX: number, outZ: number): MapCluster => ({
  departmentId: 'eng', name: 'Engineering',
  cx: outX * 46, cy: 0, cz: outZ * 46, outX, outZ,
  levelNames: [], accent: '#0ea5e9', extent: 18,
})

const near = (v: THREE.Vector3, x: number, y: number, z: number) => {
  expect(v.x).toBeCloseTo(x, 6)
  expect(v.y).toBeCloseTo(y, 6)
  expect(v.z).toBeCloseTo(z, 6)
}

describe('viewScaleFor', () => {
  it('leaves phones and small laptops at 1 and clamps big screens', () => {
    expect(viewScaleFor(390, 844)).toBe(1)
    expect(viewScaleFor(1366, 800)).toBe(1)
    expect(viewScaleFor(1920, 1080)).toBeCloseTo(1.377, 2)
    expect(viewScaleFor(3840, 2160)).toBe(VIEW_SCALE_MAX)
  })
})

describe('mapDistanceFor / mapPoseFor', () => {
  it('clamps the FOV-derived distance and scales it for big screens', () => {
    const desktop = mapDistanceFor(48, 1.6, 1)
    expect(desktop).toBeGreaterThanOrEqual(MAP_DIST_MIN)
    expect(desktop).toBeLessThanOrEqual(MAP_DIST_MAX)
    // Portrait phone: the aspect floor pushes the camera to the far clamp.
    expect(mapDistanceFor(48, 0.46, 1)).toBe(MAP_DIST_MAX)
    expect(mapDistanceFor(48, 1.6, 1.38)).toBeCloseTo(desktop * 1.38, 6)
  })

  it('stands the camera behind the department at the ground elevation', () => {
    const c = cluster(0.6, 0.8)
    const dist = mapDistanceFor(48, 1.6, 1.2)
    const pose = mapPoseFor(48, 1.6, c, 1.2)
    near(pose.cam, 0.6 * dist, dist * MAP_ELEVATION, 0.8 * dist)
    near(pose.look, 0, 0, 0)
  })
})

describe('stageExitPath', () => {
  const c = cluster(1, 0)
  // A desktop stage pose: inside the ring, low, looking outward.
  const start = new THREE.Vector3(-14, 16, 0)

  it('lands exactly on the paged whole-map pose', () => {
    // The paged machinery's goal for the same camera and viewport — the
    // flight must end where paging between departments would put the
    // camera, or the paged lerp adds a second zoom-out after landing.
    const pose = mapPoseFor(48, 1.6, c, 1.38)
    const path = stageExitPath(start, pose.cam)
    const end = path.getPoint(1)
    near(end, pose.cam.x, pose.cam.y, pose.cam.z)
    near(path.getPoint(0), start.x, start.y, start.z)
  })

  it('arcs around the ring at altitude instead of cutting through the center', () => {
    const pose = mapPoseFor(48, 1.6, c, 1)
    const path = stageExitPath(start, pose.cam)
    const ctrl = path.v1
    const rEnd = Math.hypot(pose.cam.x, pose.cam.z)
    expect(Math.hypot(ctrl.x, ctrl.z)).toBeCloseTo(
      Math.max(Math.hypot(start.x, start.z), rEnd, EXIT_ARC_MIN_RADIUS), 6,
    )
    expect(ctrl.y).toBeCloseTo(Math.max(start.y, pose.cam.y) + EXIT_ARC_LIFT, 6)
    // Every sampled point stays above the lower of the two ends.
    const floor = Math.min(start.y, pose.cam.y)
    for (let i = 0; i <= 20; i += 1) {
      expect(path.getPoint(i / 20).y).toBeGreaterThanOrEqual(floor - 1e-6)
    }
  })

  it('does not alias the caller vectors', () => {
    const end = new THREE.Vector3(120, 17, 0)
    const path = stageExitPath(start, end)
    path.v0.x = 999
    path.v2.x = 999
    expect(start.x).toBe(-14)
    expect(end.x).toBe(120)
  })
})
