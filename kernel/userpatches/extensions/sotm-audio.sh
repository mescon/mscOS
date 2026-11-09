# sMS-200Ultra audio endpoint kernel: low latency, no tracing/instrumentation overhead.
# custom_kernel_config runs after Armbian's armbian_kernel_config__* hooks (which force
# RT_GROUP_SCHED for containers and FTRACE/KPROBES for eBPF tooling) and can undo them.
# AUDIT stays (selected by AppArmor); it is switched off at boot with audit=0.
function custom_kernel_config__sotm_audio() {
	opts_y+=("PREEMPT" "HZ_1000")
	opts_n+=("PREEMPT_NONE" "PREEMPT_VOLUNTARY" "HZ_250" "DEBUG_PREEMPT"
		"RT_GROUP_SCHED" "PSI" "SCHED_AUTOGROUP" "INIT_ON_ALLOC_DEFAULT_ON"
		"FTRACE" "FUNCTION_TRACER" "FUNCTION_GRAPH_TRACER" "DYNAMIC_FTRACE" "FTRACE_SYSCALLS"
		"KPROBES" "KPROBE_EVENTS" "BPF_KPROBE_OVERRIDE" "BPF_EVENTS")
}
