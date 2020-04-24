
import torch
from torch.utils.cpp_extension import load
optix_include = "/root/workspace/docker/build/DR/NVIDIA-OptiX-SDK-6.5.0-linux64/include"
optix_ld = "/root/workspace/docker/build/DR/NVIDIA-OptiX-SDK-6.5.0-linux64/lib64"

optix = load(name="optix", sources=["/root/workspace/DR/optix_extend.cpp"],
    extra_include_paths=[optix_include], extra_ldflags=["-L"+optix_ld, "-loptix_prime"])

import trimesh
import trimesh.transformations as TF
import kornia
import numpy as np
import imageio
import random
from PIL import Image



debug = False
#render resolution
res=512
Float = torch.float64
device='cuda'
extIOR, intIOR = 1.0, 1.5
# extIOR, intIOR = 1.0, 1.15

@torch.jit.script
def dot(v1:torch.Tensor, v2:torch.Tensor, keepdim:bool = False):
    ''' v1, v2: [n,3]'''
    result = v1[:,0]*v2[:,0] + v1[:,1]*v2[:,1] + v1[:,2]*v2[:,2]
    if keepdim:
        return result.view(-1,1)
    return result

@torch.jit.script
def Reflect(wo, n):
    return -wo + 2 * dot(wo, n, True) * n

@torch.jit.script
def Refract(wo:torch.Tensor, n, eta):
    eta = eta.view(-1,1)
    cosThetaI = dot(n, wo, True)
    sin2ThetaI = (1 - cosThetaI * cosThetaI).clamp(min = 0)
    sin2ThetaT = eta * eta * sin2ThetaI
    totalInerR = (sin2ThetaT >= 1).view(-1)
    cosThetaT = torch.sqrt(1 - sin2ThetaI.clamp(max = 1))
    wt = eta * -wo + (eta * cosThetaI - cosThetaT) * n

    # wt should be already unit length, Numerical error?
    wt = wt / wt.norm(p=2, dim=1, keepdim=True).detach()

    return totalInerR, wt

@torch.jit.script
def FrDielectric(cosThetaI:torch.Tensor, etaI, etaT):

    sinThetaI = torch.sqrt( (1-cosThetaI*cosThetaI).clamp(0, 1))
    sinThetaT = sinThetaI * etaI / etaT
    totalInerR = sinThetaT >= 1
    cosThetaT = torch.sqrt( (1-sinThetaT*sinThetaT).clamp(min = 0))
    Rparl = ((etaT * cosThetaI) - (etaI * cosThetaT)) / ((etaT * cosThetaI) + (etaI * cosThetaT))
    Rperp = ((etaI * cosThetaI) - (etaT * cosThetaT)) / ((etaI * cosThetaI) + (etaT * cosThetaT))
    R = (Rparl * Rparl + Rperp * Rperp) / 2
    return totalInerR, R


@torch.jit.script
def JIT_Dintersect(origin:torch.Tensor, ray_dir:torch.Tensor, hitted:torch.Tensor, faces:torch.Tensor, vertices:torch.Tensor):
    '''
        differentiable ray-triangle intersection
        # <Fast, Minimum Storage Ray/Triangle Intersection>
        # https://cadxfem.org/inf/Fast%20MinimumStorage%20RayTriangle%20Intersection.pdf
    '''
    v0 = vertices[faces[:,0]]
    v1 = vertices[faces[:,1]]
    v2 = vertices[faces[:,2]]
    # Find vectors for two edges sharing v[0]
    edge1 = v1-v0
    edge2 = v2-v0
    n = torch.cross(edge1, edge2)
    # n = n / n.norm(dim=1).view(-1,1)
    n = n / n.norm(dim=1, p=2, keepdim=True).detach()
    pvec = torch.cross(ray_dir[hitted], edge2)
    # If determinant is near zero, ray lies in plane of triangle
    det = dot(edge1, pvec)
    inv_det = 1/det
    # # Calculate distance from v[0] to ray origin
    tvec = origin[hitted] - v0
    # Calculate U parameter
    u = dot(tvec, pvec) * inv_det
    qvec = torch.cross(tvec, edge1)
    # Calculate V parameter
    v = dot(ray_dir[hitted], qvec) * inv_det
    # Calculate T
    t = dot(edge2, qvec) * inv_det
    # A = torch.stack( (-edge1,-edge2,ray_dir[hitted]), dim=2)
    # B = -tvec.view((-1,3,1))
    # X, LU = torch.solve(B, A)
    # u = X[:,0,0]
    # v = X[:,1,0]
    # t = X[:,2,0]
    # assert v.max()<=1.001 and v.min()>=-0.001 , (v.max().item() ,v.min().item() )
    # assert u.max()<=1.001 and u.min()>=-0.001 , (u.max().item() ,u.min().item() )
    # assert (v+u).max()<=1.001 and (v+u).min()>=-0.001
    # assert t.min()>0, (t<0).sum()
    # if(t.min()<0): print((t<0).sum())
    return u, v, t, n, hitted


class primary_edge_sample(torch.autograd.Function):
    @staticmethod
    def forward(ctx, E_pos, intersect_fun, camera_M, ray_origin):
        num = len(E_pos)
        R, K, R_inverse, K_inverse = camera_M
        # E_pos [nx2x2]
        ax = E_pos[:,0,0]
        ay = E_pos[:,0,1]
        bx = E_pos[:,1,0]
        by = E_pos[:,1,1]
        #  just sample mid point for now
        x = (ax+bx)/2
        y = (ay+by)/2
        mid_point = torch.stack((x,y), dim=1) #[nx2]
        index = mid_point.to(torch.long)
        output = 0.5 * torch.ones(num, device=device) #[n]
        ctx.mark_non_differentiable(index)

        # α(x, y) = (ay - by)x + (bx - ax)y + (axby - bxay)
        Nx = ay-by # (ay - by)x
        Ny = bx-ax # (bx - ax)y
        N = torch.stack((Nx,Ny), dim=1) #[nx2]
        normalized_N = N / torch.norm(N, dim=1).view(-1,1)
        length = torch.norm( E_pos[:,0]-E_pos[:,1] , dim=1)
        eps = 1e-5
        # eps = 1
        fu_point = (mid_point + eps*normalized_N).T #[2xn]
        fl_point = (mid_point - eps*normalized_N).T #[2xn]

        f_point = torch.cat((fu_point,fl_point), dim=1) #[2x2n]
        W = torch.ones([1, f_point.shape[1]], dtype=Float, device=device)
        camera_p = K_inverse @ torch.cat([-f_point, -W], dim=0) # pixel at z=-1
        camera_p = torch.cat([camera_p, W], dim=0)
        world_p = R @ camera_p #[4x2n]
        world_p = world_p[:3].T #[2nx3]
        ray_dir = world_p - ray_origin.view(-1,3)
        ray_origin = ray_origin.expand_as(ray_dir)
        hitted, _ = intersect_fun(ray_origin, ray_dir)
        mask = torch.zeros(2*num, device=device)
        mask[hitted] = 1
        f = mask[:num] - mask[num:]

        # #==========fu==============
        # W = torch.ones([1, fu_point.shape[1]], dtype=Float, device=device)
        # camera_p = K_inverse @ torch.cat([-fu_point, -W], dim=0) # pixel at z=-1
        # camera_p = torch.cat([camera_p, W], dim=0)
        # world_p = R @ camera_p #[4xn]
        # world_p = world_p[:3].T #[nx3]
        # ray_dir = world_p - ray_origin.view(-1,3)
        # ray_origin = ray_origin.expand_as(ray_dir)
        # hittedu, _ = intersect_fun(ray_origin, ray_dir)


        # #==========fl==============
        # W = torch.ones([1, fl_point.shape[1]], dtype=Float, device=device)
        # camera_p = K_inverse @ torch.cat([-fl_point, -W], dim=0) # pixel at z=-1
        # camera_p = torch.cat([camera_p, W], dim=0)
        # world_p = R @ camera_p #[4xn]
        # world_p = world_p[:3].T #[nx3]
        # ray_dir = world_p - ray_origin.view(-1,3)
        # ray_origin = ray_origin.expand_as(ray_dir)
        # hittedl, _ = intersect_fun(ray_origin, ray_dir)

        # masku = torch.zeros(num, device=device)
        # maskl = torch.zeros(num, device=device)
        # masku[hittedu]=1
        # maskl[hittedl]=1
        # f = masku - maskl

        denominator = torch.sqrt(N.pow(2).sum(dim=1))
        dax = by - y
        dbx = y - ay
        day = x - bx
        dby = ax - x
        dx = torch.stack((dax,dbx),dim=1)
        dy = torch.stack((day,dby),dim=1)
        dE_pos = torch.stack((dx,dy),dim=2) #[nx2x2]
        dE_pos = dE_pos * (length * f / denominator).view(-1,1,1)
        ctx.save_for_backward(dE_pos/res)

        return index, output
        # ctx.mark_non_differentiable(f)
        # return index, output, f

    @staticmethod
    # def backward(ctx, grad_index, grad_output, grad_f):
    def backward(ctx, grad_index, grad_output):
        dE_pos = ctx.saved_variables[0]
        grad = dE_pos * grad_output.view(-1,1,1)
        # print("backward")
        return grad, None, None, None, None


class Scene:
    def __init__(self, mesh_path):
        mesh = trimesh.load(mesh_path, process=False)
        # assert mesh.is_watertight
        self.mesh = mesh
        self.vertices = torch.tensor(mesh.vertices, dtype=Float, device=device)
        self.faces = torch.tensor(mesh.faces, dtype=torch.long, device=device)
        opt_v = self.vertices.to(torch.float32).to(device)
        opt_F = self.faces.to(torch.int32).to(device)
        self.optix_mesh = optix.optix_mesh(opt_F, opt_v)
        self.scene = mesh.scene()
        self.init_weightM()
        self.init_edge()

    def init_edge(self):
        '''
        # Calculate E2V_index for silhouette detection
        '''
        mesh = self.mesh
        # require_count=2 means edge with exactly two face (watertight edge)
        Egroups = trimesh.grouping.group_rows(mesh.edges_sorted, )
        # unique, undirectional edges
        edges = mesh.edges_sorted[Egroups[:,0]]
        Edges = torch.tensor(edges, device=device)
        E2F_index = mesh.edges_face[Egroups] #[Ex2]
        E2F = mesh.faces[E2F_index] #[Ex2x3]
        self.Edges = Edges
        self.E2F = E2F

    def init_weightM(self):
        '''
        # Calculate a sparse matrix for laplacian operations
        '''
        neighbors = self.mesh.vertex_neighbors
        col = np.concatenate(neighbors)
        row = np.concatenate([[i] * len(n) for i, n in enumerate(neighbors)])
        weight = np.concatenate([[1.0 / len(n)] * len(n) for n in neighbors])
        col = torch.tensor(col, device=device)
        row = torch.tensor(row, device=device)
        coo = torch.stack((row,col))
        weight = torch.tensor(weight, dtype=Float, device=device)
        size = len(self.vertices)
        self.weightM = torch.sparse.FloatTensor(coo, weight, torch.Size([size, size]))

    def update_verticex(self, vertices:torch.Tensor):
        opt_v = vertices.to(torch.float32).to(device)
        self.optix_mesh.update_vert(opt_v)
        self.mesh.vertices = vertices.detach().cpu().numpy()
        self.vertices = vertices

    def opt_intersect(self, origin:torch.Tensor, ray_dir:torch.Tensor):
        optix_o = origin.to(torch.float32).to(device)
        optix_d = ray_dir.to(torch.float32).to(device)
        Ray = torch.cat([optix_o, optix_d], dim=1)
        T, ind = self.optix_mesh.intersect(Ray)
        hitted = T>0
        faces = self.faces[(ind[hitted].to(torch.long))]
        hitted = torch.nonzero(hitted).squeeze()
        return hitted, faces

    # def apply_transform(self, matrix):
    #     self.mesh.apply_transform(matrix)
    #     self.vertices = torch.tensor(self.mesh.vertices, dtype=Float, device=device)

    def laplac_hook(self, grad):
        # print("hook")
        vertices = self.vertices.detach()
        laplac = vertices - self.weightM.mm(vertices) 
        self.hook_rough = torch.norm(laplac, dim=1).abs().mean().item()
        print(self.hook_rough, torch.norm(grad, dim=1).abs().mean().item())
        return self.hook_w * laplac + grad

    def laplac_normal_hook(self, grad):
        vertices = self.vertices.detach()
        laplac = vertices - self.weightM.mm(vertices) 
        laplac = (laplac * self.hook_normal).sum(dim=1, keepdim=True)
        self.hook_rough = laplac.abs().mean().item()
        laplac[laplac.abs()<0.005]=0
        # print(laplac.shape, grad.shape)
        return self.hook_w * laplac + grad



    def render_transparent(self, origin:torch.Tensor, ray_dir:torch.Tensor):
        image = torch.zeros(ray_dir.shape, dtype=Float, device=device)
        ind, color = self.trace2(origin, ray_dir)
        image[ind]=color
        image_mask = torch.zeros(ray_dir.shape, dtype=torch.bool, device=device)
        image_mask[ind] = True
        return image, image_mask

    def mask(self, origin:torch.Tensor, ray_dir:torch.Tensor):
        hitted, faces = self.opt_intersect(origin, ray_dir)
        image = torch.zeros((ray_dir.shape[0]), dtype=Float, device=device)
        image[hitted] = 1
        return image
        
    def silhouette_edge(self, origin:torch.Tensor):
        vertices = self.vertices #[Vx3]
        faces = self.E2F #[Ex2x3]
        v0 = vertices[faces[:,0,0]]
        v1 = vertices[faces[:,0,1]]
        v2 = vertices[faces[:,0,2]]
        N1 = torch.cross(v1-v0, v2-v0) #[Ex3]
        N1 = N1 / N1.norm(dim=1).view(-1,1)
        dir = origin - v0
        dot1 = dot(N1, dir)

        v0 = vertices[faces[:,1,0]]
        v1 = vertices[faces[:,1,1]]
        v2 = vertices[faces[:,1,2]]
        N2 = torch.cross(v1-v0, v2-v0) #[Ex3]    
        N2 = N2 / N2.norm(dim=1).view(-1,1)
        dir = origin - v0
        dot2 = dot(N2, dir)

        silhouette = torch.logical_xor(dot1>0,dot2>0)
        return self.Edges[silhouette]

    def primary_visibility(self, silhouette_edge, camera_M, origin, detach_depth = False):
        '''
            detach_depth: bool
            detach_depth means we don't want the gradient rwt the depth coordinate
        '''
        R, K, R_inverse, K_inverse = camera_M

        V = self.vertices[silhouette_edge.view(-1)] #[2Nx3]
        W = torch.ones([V.shape[0],1], dtype=Float, device=device)
        hemo_v = torch.cat([V, W], dim=1) #[2Nx4]
        v_camera =  R_inverse @ hemo_v.T #[4x2N]
        if detach_depth: 
            v_camera[2:3] = v_camera[2:3].detach()
        v_camera = K @ v_camera[:3] #[3x2N]
        pixel_index = v_camera[:2] / v_camera[2]  #[2x2N]
        E_pos = pixel_index.T.reshape(-1,2,2)
        index, output = primary_edge_sample.apply(E_pos, self.opt_intersect, camera_M, origin) #[Nx2]

        index[:,0] = res-1-index[:,0]
        #out of view
        mask = (index[:,0] < res-1) * (index[:,1] < res-1) * (index[:,0] >= 0) * (index[:,1] >= 0)
        return index[mask], output[mask]

    def project_vert(self, camera_M, V:torch.Tensor):
        R, K, R_inverse, K_inverse = camera_M

        W = torch.ones([V.shape[0],1], dtype=Float, device=device)
        hemo_v = torch.cat([V, W], dim=1) #[Nx4]
        v_camera = R_inverse @ hemo_v.T #[3xN]
        v_camera = K @ v_camera[:3]
        pixel_index = v_camera[:2] / v_camera[2]
        pixel_index = pixel_index.to(torch.long)
        pixel_index[0] = res-1 - pixel_index[0]
        return pixel_index
    def set_camera(self, fov, distance, center, angles):
        self.scene.set_camera(resolution=(res,res), fov=fov, distance = distance, center=center, angles=angles)
    def camera_M(self):
        scene = self.scene
        R = torch.tensor(scene.camera_transform, dtype=Float, device=device)
        K = torch.tensor(scene.camera.K, dtype=Float, device=device)
        R_inverse = torch.inverse(R)
        K_inverse = torch.inverse(K)
        return R, K, R_inverse, K_inverse
    def generate_ray(self):
        scene = self.scene
        origin, ray_dir, _ = scene.camera_rays()
        origin = torch.tensor(origin, dtype=Float, device=device)
        ray_dir = torch.tensor(ray_dir, dtype=Float, device=device)
        return origin, ray_dir

    # def Dintersect(self, origin:torch.Tensor, ray_dir:torch.Tensor):
    #     hitted, faces = self.opt_intersect(origin, ray_dir)

    #     # <<Fast, Minimum Storage Ray/Triangle Intersection>> 
    #     # https://cadxfem.org/inf/Fast%20MinimumStorage%20RayTriangle%20Intersection.pdf
    #     vertices = self.vertices
    #     v0 = vertices[faces[:,0]]
    #     v1 = vertices[faces[:,1]]
    #     v2 = vertices[faces[:,2]]

    #     # Find vectors for two edges sharing v[0]
    #     edge1 = v1-v0
    #     edge2 = v2-v0
    #     n = torch.cross(edge1, edge2)
    #     # n = n / n.norm(dim=1).view(-1,1)
    #     n = n / n.norm(dim=1, p=2).view(-1,1).detach()

    #     pvec = torch.cross(ray_dir[hitted], edge2)
    #     # If determinant is near zero, ray lies in plane of triangle
    #     det = dot(edge1, pvec)
    #     inv_det = 1/det
    #     # # Calculate distance from v[0] to ray origin
    #     tvec = origin[hitted] - v0
    #     # Calculate U parameter
    #     u = dot(tvec, pvec) * inv_det
    #     qvec = torch.cross(tvec, edge1)
    #     # Calculate V parameter
    #     v = dot(ray_dir[hitted], qvec) * inv_det
    #     # Calculate T
    #     t = dot(edge2, qvec) * inv_det

    #     # A = torch.stack( (-edge1,-edge2,ray_dir[hitted]), dim=2)
    #     # B = -tvec.view((-1,3,1))
    #     # X, LU = torch.solve(B, A)
    #     # u = X[:,0,0]
    #     # v = X[:,1,0]
    #     # t = X[:,2,0]
    #     # assert v.max()<=1.001 and v.min()>=-0.001 , (v.max().item() ,v.min().item() )
    #     # assert u.max()<=1.001 and u.min()>=-0.001 , (u.max().item() ,u.min().item() )
    #     # assert (v+u).max()<=1.001 and (v+u).min()>=-0.001
    #     # assert t.min()>0, (t<0).sum()
    #     # if(t.min()<0): print((t<0).sum())

    #     return u, v, t, n, hitted

    def Dintersect(self, origin:torch.Tensor, ray_dir:torch.Tensor):
        hitted, faces = self.opt_intersect(origin, ray_dir)
        vertices = self.vertices
        return JIT_Dintersect(origin,ray_dir,hitted,faces,vertices)



    def trace2(self, origin, ray_dir, depth=1, santy_check=False):
        def debug_cos():
            index = cosThetaI.argmax()
            return wo[index].item() , n[index].item() 
        if (depth <= 2):
            # etaI, etaT = extIOR, intIOR
            u, v, t, n, hitted = self.Dintersect(origin, ray_dir)
            if debug:
                if (depth==2 and not (len(hitted)==len(ray_dir))):
                    print(len(ray_dir)-len(hitted), "inner object ray miss")
            wo = -ray_dir[hitted]
            cosThetaI = dot(wo, n)
            # print("max={},min={}".format(cosThetaI.max(), cosThetaI.min()))
            assert cosThetaI.max()<=1.00001 and cosThetaI.min()>=-1.00001, "wo={},n={}".format(*debug_cos())
            cosThetaI = cosThetaI.clamp(-1, 1)
            entering = cosThetaI > 0
            if debug:
                if depth==1 and not entering.all():
                    print(torch.logical_not(entering).sum().item(), "normal may be wrong")
                elif depth==2 and not torch.logical_not(entering).all():
                    print(entering.sum().item(), "inner object ray don't shot out")
            # assert(entering.all() or torch.logical_not(entering).all()),entering.sum()
            # etaI, etaT = extIOR*torch.ones_like(hitted), intIOR*torch.ones_like(hitted)
            # if not entering.all(): 
            #     etaI, etaT = etaT, etaI
            #     n = -n
            #     cosThetaI = -cosThetaI
            exc = torch.logical_not(entering)
            etaI, etaT = extIOR*torch.ones_like(hitted), intIOR*torch.ones_like(hitted)
            etaI[exc], etaT[exc] = etaT[exc], etaI[exc]
            n[exc] = -n[exc]
            cosThetaI[exc] = -cosThetaI[exc]  

            totalInerR1, R = FrDielectric(cosThetaI, etaI, etaT)
            wr = Reflect(wo, n)
            totalInerR2, wt = Refract(wo, n, etaI/etaT)
            # print(totalInerR1.shape, totalInerR2.shape)
            if debug:
                assert (totalInerR1 == totalInerR2).all(), (totalInerR1 != totalInerR2).sum()
            refracted = torch.logical_not(totalInerR1)
            # refracted = torch.ones(totalInerR1.shape[0])>0

            # print(t.shape, ray_dir[hitted].shape)
            new_origin = origin[hitted][refracted] + t[refracted].view(-1,1) * ray_dir[hitted][refracted]
            new_dir = wt[refracted]
            # new_dir = wr[refracted]

            # embree seems to miss epsilon check to avoid self intersection?
            # TODO: a better way to determine epsilon(1e-5)
            new_origin += 1e-5 * new_dir

            index, color = self.trace2(new_origin, new_dir, depth+1, santy_check)
            # index, color = trace2(vertices.detach(), mesh, new_origin, new_dir, depth+1, santy_check)
            return hitted[refracted][index], color
        else:
            if santy_check:
                return torch.ones(ray_dir.shape[0])>0, origin
            else:
                #return if hit nothing
                optix_o = origin.to(torch.float32).to(device)
                optix_d = ray_dir.to(torch.float32).to(device)
                Ray = torch.cat([optix_o,optix_d], dim=1)
                T, ind = self.optix_mesh.intersect(Ray)
                missed = T<0
                missed = torch.nonzero(missed).squeeze()
                return missed, ray_dir[missed]

def save_torch(name, img:torch.Tensor):
    image = (255 * (img-img.min()) / (img.max()-img.min())).to(torch.uint8)
    imageio.imsave(name, image.view(res,res,-1).permute(1,0,2).cpu())

def torch2pil(img:torch.Tensor):
    image = (255 * (img-img.min()) / (img.max()-img.min())).to(torch.uint8)
    image = image.view(res,res,-1).permute(1,0,2).cpu().numpy()
    if image.shape[2] == 1: image = image[:,:,0]
    return Image.fromarray(image)