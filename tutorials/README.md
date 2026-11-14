# Cleaner Robot Tutorial

This directory is the public runnable R2Kernel tutorial. It builds one composed
cleaner robot in a provided 2D simulation world, then runs applications against
that robot through R2Kernel subsystem, package, binding, and deployment flows.

The tutorial root is intentionally flat so installed users can start from
`<prefix>/tutorials` without first choosing between multiple example trees.
`<prefix>` is the install prefix (`/opt/r2kernel` by default).

## Layout

- `run_sim_world.py`
  - starts the provided simulator and browser world viewer
- `frontend/`
  - static browser view for the tutorial world
- `simulator/`
  - provided 2D world and support API
- `subsystems/`
  - gateway, component, compound, and app subsystem package roots
- `lib/`
  - shared Python modules used by the tutorial subsystems and simulator
- `spec/`
  - package-local tutorial API contracts outside the official dictionary
- `config/`
  - tutorial fixture configuration
- `deployments/`
  - staged and full deployment manifests
- `learning/radar_policy/`
  - CPU training and held-out closed-loop evaluation for the learned radar policy
- `robot/`
  - robot platform declaration material
- `advanced/resource_control/`
  - advanced host resource-control variants for the same tutorial environment
- `support/`
  - helper code used by tutorial, benchmark, and QA commands, including the
    C++ fixture build CLI
- `CMakeLists.txt`
  - C++ support target and transport fixture executables
- `tests/`
  - tutorial-local simulation tests

Low-level declaration and transport fixtures are not part of the user tutorial
surface. They live under `fixtures/` in the source workspace and are published
as `tools/qa/fixtures` for release regression and benchmark support.

## Start

From an installed runtime:

```bash
. <prefix>/env.sh
cd <prefix>
python3 tutorials/run_sim_world.py
```

Then follow the documentation under **Develop -> Cleaner Robot Tutorial**.

For editable development, copy `<prefix>/tutorials` to a writable working
directory or work from the source checkout.

## Model-based radar application

`app.cleaner.learned_radar` is a behavior-cloning MLP that consumes the same
0.30 m radar observations as the rule-based radar application. When no target
is visible it generates active-exploration body-motion references; after a
target is detected it generates approach references. The app calls the mobile
component's velocity-reference Procedure through the compound export; the
component alone converts accepted references into timed gateway Control
commands. Grasping and disposal use the existing high-level APIs.

Select `deployments/learned-radar.deployment.yaml` to run this application
with the installed runtime. In the frontend,
choose **Reset with far target** to start with the only target outside radar
range. The Learned Policy panel separates the model-proposed reference from the
value accepted by `component.mobile`.
