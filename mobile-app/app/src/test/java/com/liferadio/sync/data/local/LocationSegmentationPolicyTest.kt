package com.liferadio.sync.data.local

import org.junit.Assert.assertEquals
import org.junit.Test

class LocationSegmentationPolicyTest {
    @Test fun startsFirstSample() {
        assertEquals(SegmentPlan(SegmentAction.START, "sample"), LocationSegmentationPolicy.plan(false, 0f, 0))
    }

    @Test fun updatesSameSegmentInsideRadiusAndPromotesToStay() {
        assertEquals(SegmentPlan(SegmentAction.UPDATE, "sample"), LocationSegmentationPolicy.plan(true, 149.9f, 899))
        assertEquals(SegmentPlan(SegmentAction.UPDATE, "stay"), LocationSegmentationPolicy.plan(true, 150f, 900))
    }

    @Test fun finalizesOldSegmentWhenMovingOutsideRadius() {
        assertEquals(
            SegmentPlan(SegmentAction.FINALIZE_AND_START, "sample"),
            LocationSegmentationPolicy.plan(true, 150.1f, 1200)
        )
    }

    @Test fun finalPayloadAdvancesRevisionWithoutChangingSemanticEndTime() {
        assertEquals(1_001L, LocationSegmentationPolicy.finalRevision(1_000L))
    }

    @Test fun ignoresCachedOrOutOfOrderFixForActiveSegment() {
        assertEquals(false, LocationSegmentationPolicy.acceptsForActiveSegment(999L, 1_000L))
        assertEquals(false, LocationSegmentationPolicy.acceptsForActiveSegment(1_000L, 1_000L))
        assertEquals(true, LocationSegmentationPolicy.acceptsForActiveSegment(1_001L, 1_000L))
    }
}
