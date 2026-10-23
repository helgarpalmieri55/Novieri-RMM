//go:build linux

package backup

import "golang.org/x/sys/unix"

// isPseudoFS reports a filesystem that holds no data, only kernel interfaces.
// A live read of `/` would otherwise walk into /proc and block forever on
// /proc/kmsg. Asked only where the walk crosses onto another device.
func isPseudoFS(p string) bool {
	var st unix.Statfs_t
	if err := unix.Statfs(p, &st); err != nil {
		return false
	}
	switch int64(st.Type) { //nolint:unconvert // int32 on some architectures
	case unix.PROC_SUPER_MAGIC, unix.SYSFS_MAGIC, unix.DEVPTS_SUPER_MAGIC, unix.CGROUP_SUPER_MAGIC,
		unix.CGROUP2_SUPER_MAGIC, unix.SECURITYFS_MAGIC, unix.DEBUGFS_MAGIC, unix.TRACEFS_MAGIC,
		unix.PSTOREFS_MAGIC, unix.BPF_FS_MAGIC, unix.EFIVARFS_MAGIC, unix.NSFS_MAGIC, unix.BINFMTFS_MAGIC,
		unix.HUGETLBFS_MAGIC, unix.AUTOFS_SUPER_MAGIC, unix.SELINUX_MAGIC, unix.SMACK_MAGIC:
		return true
	}
	return false
}
