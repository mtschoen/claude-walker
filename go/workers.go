package main

const maximumWorkerCount = 8

func effectiveWorkerCount(processorCount int) int {
	if processorCount > maximumWorkerCount {
		return maximumWorkerCount
	}
	return processorCount
}

func effectiveEventWorkerCount(processorCount, groupCount int) int {
	workerCount := effectiveWorkerCount(processorCount)
	if groupCount > 0 && workerCount > groupCount {
		return groupCount
	}
	return workerCount
}
