"""Rotation and homogeneous-transform helpers (radians, meters).

SciPy is an optional dependency of the SDK; install it with the ``rotation`` extra
(``pip install 'r2kernel-client[rotation]'``) to use this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from scipy.spatial.transform import Rotation as R


def _rotation_class():
    try:
        from scipy.spatial.transform import Rotation
    except ImportError as exc:
        raise ImportError(
            "r2kernel.utility.rotation_transform requires SciPy; "
            "install it with: pip install 'r2kernel-client[rotation]'"
        ) from exc
    return Rotation

# 속도비교 결과 scipy 내장 함수를 쓰는것이, np로 직접계산하는 것보다 3배정도 빠름..


###############################################################################
# 1) RPY <-> Rotation object
###############################################################################

# def rpy_to_rotation(roll: float, pitch: float, yaw: float, degrees: bool = False) -> R:
#     """
#     RPY(Roll, Pitch, Yaw)를 이용해 Rotation 객체 생성.
#     roll, pitch, yaw는 각각 x, y, z축 순서로 회전.
#     degrees=True시 입력을 deg(도)로, False시 rad(라디안)으로 처리.
#     """
#     return R.from_euler('xyz', [roll, pitch, yaw], degrees=degrees)

# def rotation_to_rpy(rotation: R, degrees: bool = False) -> np.ndarray:
#     """
#     Rotation 객체에서 RPY(Roll, Pitch, Yaw) 추출.
#     반환되는 배열은 [roll, pitch, yaw].
#     degrees=True시 결과를 deg(도)로 반환.
#     """
#     return rotation.as_euler('xyz', degrees=degrees)

def rpy_to_rotation(roll: float, pitch: float, yaw: float) -> R:
    """
    사용자가 'roll, pitch, yaw' 라고 부르는 각들을
    실제로는 z->y->x 순 (Intrinsic)으로 회전한 Rotation으로 변환.

    - roll 은 x축 회전
    - pitch 는 y축 회전
    - yaw 는 z축 회전
    - 입력 각도는 라디안 단위
    """
    # 실제로는 z→y→x 순이므로, 인자는 [yaw, pitch, roll] 순서로 넣는다.
    # seq='zyx' → 첫 회전 z, 두 번째 y, 세 번째 x (모두 로컬 축)
    rot = _rotation_class().from_euler('ZYX', [yaw, pitch, roll], degrees=False)
    return rot


def rotation_to_rpy(rotation: R) -> tuple[float, float, float]:
    """
    Rotation 객체가 "z->y->x (Intrinsic)" 순으로 회전했다고 가정하고,
    이를 (roll, pitch, yaw) 순으로 되돌려서 리턴한다.

    - 반환 순서는 (roll, pitch, yaw)
    - 반환 각도는 라디안 단위
    """
    # as_euler('zyx') → [yaw, pitch, roll] 을 리턴 (Intrinsic)
    y, p, r = rotation.as_euler('ZYX', degrees=False)  # rad단위로 뽑아옴

    # 순서를 (roll, pitch, yaw)로 맞춰 리턴
    return (r, p, y)

###############################################################################
# 2) ZYZ Euler <-> Rotation object
###############################################################################


def zyz_to_rotation(z1: float, y: float, z2: float) -> R:
    """
    ZYZ 오일러 각도를 이용해 Rotation 객체 생성.
    z1 -> y -> z2 순서로 회전.
    입력 각도는 라디안 단위.
    """
    return _rotation_class().from_euler('zyz', [z2, y, z1], degrees=False)


def rotation_to_zyz(rotation: R) -> np.ndarray:
    """
    Rotation 객체에서 ZYZ 오일러 각도(z1, y, z2) 추출.
    반환 각도는 라디안 단위.
    """
    angles = rotation.as_euler('zyz', degrees=False)
    return angles[::-1]

###############################################################################
# 3) Quaternion <-> Rotation object
###############################################################################
# SciPy에서는 quaternion을 [x, y, z, w] 순서로 다룹니다.


def quaternion_to_rotation(quaternion: np.ndarray) -> R:
    """
    Quaternion([x, y, z, w]) -> Rotation 객체.
    만약 로보틱스에서 [w, x, y, z] 순서를 사용 중이라면
    입력에 맞춰 재정렬해야 합니다.
    """
    # quaternion 형상/길이 확인(길이 4)
    quaternion = np.asarray(quaternion, dtype=float)
    if quaternion.shape != (4,):
        raise ValueError("quaternion must be a 4-element array [x, y, z, w].")
    return _rotation_class().from_quat(quaternion)


def rotation_to_quaternion(rotation: R) -> np.ndarray:
    """
    Rotation 객체 -> Quaternion([x, y, z, w]).
    로보틱스에서 [w, x, y, z]로 쓰고 싶다면 순서 바꿔서 사용.
    """
    return rotation.as_quat()

###############################################################################
# 4) Rotation Vector <-> Rotation object
###############################################################################


def rotvec_to_rotation(rx: float, ry: float, rz: float) -> R:
    """
    회전 벡터(Rodrigues 축-각 표현)을 
    3개의 스칼라 인자(rx, ry, rz)로 받아 Rotation 객체를 생성한다.

    Parameters
    ----------
    rx : float
        x축에 대한 회전 성분 (라디안)
    ry : float
        y축에 대한 회전 성분 (라디안)
    rz : float
        z축에 대한 회전 성분 (라디안)

    Returns
    -------
    R
        scipy의 Rotation 객체
    """
    # 3개의 스칼라를 리스트나 ndarray로 만든 뒤 from_rotvec에 전달
    rotvec = [rx, ry, rz]
    return _rotation_class().from_rotvec(rotvec)


def rotation_to_rotvec(rotation: R) -> np.ndarray:
    """
    Rotation 객체 -> 회전 벡터(Rodrigues 축-각 표현, [rx, ry, rz]).
    """
    return rotation.as_rotvec()


def convert_list_to_homogeneous(params, convention='rotvec'):
    """
    6개 원소 리스트 [tx, ty, tz, a1, a2, a3]를 4x4 homogeneous transformation matrix로 변환합니다.

    Parameters:
      - params: 리스트 혹은 array-like, [tx, ty, tz, a1, a2, a3]
                앞 3개는 translation (미터 단위),
                뒤 3개는 회전 파라미터 (rad 단위)
      - convention: 회전 파라미터 해석 방식 (문자열)
                      'rotvec' : 회전벡터 (axis-angle), 사용 함수: rotvec_to_rotation
                      'rpy'    : roll, pitch, yaw, 사용 함수: rpy_to_rotation
                      'zyz'    : ZYZ Euler 각, 사용 함수: zyz_to_rotation

    Returns:
      - T: 4x4 numpy array, homogeneous transformation matrix
    """
    if len(params) != 6:
        raise ValueError("입력 리스트는 6개의 원소를 가져야 합니다.")

    tx, ty, tz, a1, a2, a3 = params

    # convention에 따라 적절한 회전 변환 함수를 호출
    if convention == 'rotvec':
        rot_obj = rotvec_to_rotation(a1, a2, a3)
    elif convention == 'rpy':
        rot_obj = rpy_to_rotation(a1, a2, a3)
    elif convention == 'zyz':
        rot_obj = zyz_to_rotation(a1, a2, a3)
    else:
        raise ValueError("지원하지 않는 회전 컨벤션입니다. 'rotvec', 'rpy', 'zyz' 중 하나를 사용하세요.")

    # Rotation 객체를 3x3 회전 행렬로 변환
    R_matrix = rot_obj.as_matrix()

    # 4x4 homogeneous transformation matrix 생성
    T = np.eye(4)
    T[:3, :3] = R_matrix
    T[:3, 3] = np.array([tx, ty, tz])

    return T
