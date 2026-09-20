from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

import PlacementSurrogate as surrogate
from ProjectClusterInitialPlacement import project_coordinates


class PlacementSurrogateTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def log(self,path,seed,wns,tns,hpwl):
        path.write_text("[INFO] parameters = {'random_seed': %d}\n"%seed+
            '[INFO] DREAMPlace - [Final placement] wns=%s (ps_late)\n'%wns+
            '[INFO] DREAMPlace - [Final placement] tns=%s (ps_late)\n'%tns+
            '[INFO] DREAMPlace - [Final placement] hpwl=%s (weighted_length)\n'%hpwl+
            '[INFO] DREAMPlace - [Final placement] overflow=0.1 (ratio)\n')

    def test_affine_fit_and_prediction(self):
        pairs=self.root/'pairs.tsv'
        with pairs.open('w') as f:
            f.write('case_id\toriginal_log\tplacement_log\n')
            for i,c in enumerate((-40.,-30.,-20.,-10.)):
                a=2*c+3;al=self.root/f'a{i}.log';cl=self.root/f'c{i}.log'
                self.log(al,i,a,3*a,100+i*20);self.log(cl,i,c,3*c,50+i*10)
                f.write(f's{i}\t{al}\t{cl}\n')
        out=self.root/'model';args=SimpleNamespace(pairs=str(pairs),output=str(out),top_k=2)
        model=surrogate.fit(args)
        self.assertAlmostEqual(model['models']['wns']['slope'],2)
        self.assertAlmostEqual(model['models']['wns']['intercept'],3)
        new=self.root/'new.log';self.log(new,99,-25,-75,75)
        pred=self.root/'prediction.json'
        result=surrogate.predict(SimpleNamespace(model=str(out/'model.json'),placement_log=str(new),
                                                  case_id='new',output=str(pred)))
        self.assertAlmostEqual(result['predicted_A_metrics']['wns'],-47)
        self.assertTrue(pred.is_file())

    def test_area_weighted_center_projection_and_clipping(self):
        original=SimpleNamespace(num_movable_nodes=3,node_size_x=np.array([2.,2.,2.]),
            node_size_y=np.array([1.,1.,1.]))
        placement=SimpleNamespace(num_movable_nodes=2,node_size_x=np.array([4.,2.]),
            node_size_y=np.array([1.,1.]),meta=dict(xl=0.,yl=0.,xh=20.,yh=10.))
        x,y,n=project_coordinates(original,placement,np.array([0,0,1]),
                                   np.array([0.,4.,19.]),np.array([2.,2.,9.5]))
        np.testing.assert_allclose(x,[1.,18.])
        np.testing.assert_allclose(y,[2.,9.])
        self.assertEqual(n,1)

    def test_singleton_preserves_exact_lower_left_when_size_differs(self):
        original=SimpleNamespace(num_movable_nodes=1,node_size_x=np.array([2.]),node_size_y=np.array([1.]))
        placement=SimpleNamespace(num_movable_nodes=1,node_size_x=np.array([6.]),node_size_y=np.array([2.]),
                                  meta=dict(xl=0.,yl=0.,xh=20.,yh=10.))
        x,y,n=project_coordinates(original,placement,np.array([0]),np.array([7.]),np.array([3.]))
        np.testing.assert_allclose([x[0],y[0]],[7.,3.]);self.assertEqual(n,0)


if __name__=='__main__':unittest.main()
