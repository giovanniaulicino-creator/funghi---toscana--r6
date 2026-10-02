from funghi_r6.assemble import coverage


def test_rows_alone_do_not_mean_complete():
    rows=[{"code":f"TOS{i:08d}","lat":43.0,"lon":11.0} for i in range(418)]
    cov=coverage(rows)
    assert cov["structural"] == 418
    assert cov["scientific_complete"] == 0
